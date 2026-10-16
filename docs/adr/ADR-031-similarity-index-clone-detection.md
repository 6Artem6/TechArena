# ADR-031: SimilarityIndex — 4-уровневое обнаружение клонов файлов

**Статус:** Принято  
**Дата:** 2026-10-14  
**Контекст:** Blast Radius Engine, pre-processing слой перед построением графа

---

## Контекст и проблема

В train-ticket `SecurityConfig.java` присутствует в 39 сервисах — почти идентичные файлы.
`InitData.java`, `Queues.java`, `Travel.java` — ещё несколько группы структурных клонов.

**Проблема для blast radius:** если файл A изменился, а файл B является его клоном, потребитель
должен явно знать об этом факте. Движок не должен автоматически «транслировать» изменение в B,
но должен предоставить информацию: «B похож на A на уровне `ast_exact`».

**Задачи, которые решает SimilarityIndex:**
1. Обнаружение групп клонов для визуализации и отчётов.
2. Поиск похожих файлов по запросу (query-time).
3. Предоставление сигнала для `SIBLING_OF` рёбер в графе (ADR-027).
4. Быстрая идентификация: изменился ли файл (сравнение хэша без чтения диска).

---

## Рассмотренные варианты

| Вариант | Плюсы | Минусы |
|---|---|---|
| Только SHA-256 | Просто, быстро | Не ловит структурные клоны с разными литералами |
| diff-based (git) | Точно | Нет git в offline-окружении, медленно |
| BGE-M3 эмбеддинги | Семантически богаты | Tier 3 (опционально), большой overhead |
| **n-граммы AST + 4 уровня** | Без зависимостей, 635 файлов за 0.31с | Приближённое сравнение |

---

## Решение

Модуль `src/similarity/` — `SimilarityIndex` с четырьмя независимыми уровнями обнаружения.
Уровни не вытесняют друг друга в индексе, но при `query()` более точный вытесняет более слабый
для одного и того же файла.

### Уровни (в порядке строгости сравнения)

| # | Уровень | Метод | Сложность query | Confidence |
|---|---|---|---|---|
| 1 | `hash` | SHA-256 контента | O(1) | 1.0 |
| 2 | `body_exact` | SHA-256 тела без комментариев и импортов | O(1) | 0.92 |
| 3 | `ast_exact` | SHA-256 нормализованного скелета (только сигнатуры) | O(1) | 0.95 |
| 4 | `ast_vector` | cosine(n-грамм скелета), порог 0.70 | O(n) | [0.70, 1.0) |
| 5 | `ctx_vector` | cosine(n-грамм контекста), порог 0.55 | O(n) | [0.55, 1.0) |

**Иерархия строгости:** `hash ⊃ body_exact ⊃ ast_exact`

Если два файла — `body_exact` клоны, то они и `ast_exact` клоны.
Обратное неверно: `ast_exact` клоны с разными `body_fp` — одинаковые сигнатуры, но разные тела методов.

### Body_exact — тело без шума

`_strip_noise(content, lang)` удаляет по языку:
- Java: `/* */`, `/** */`, `//`, строки `import …;`, `package …;`
- Python: `"""`, `'''`, `#`, строки `import`, `from … import`
- Go/JS: аналогично

Два файла с одинаковым `body_fp` отличаются только шапкой.
На train-ticket (479 файлов): **9 body_exact групп** — в них `Travel.java`, `Queues.java`, `AdminTrip.java` и др.

### ast_exact клоны с разными body_fp — намеренная кастомизация

`body_subgroups()` разбивает каждую ast_exact группу на body_exact подгруппы:

```
SecurityConfig.java × 29  →  29 вариантов тела  (каждый сервис кастомизировал antMatchers)
Queues.java         × 5   →  2 варианта тела:
    Вариант 1: [ts-notification-service, ts-preserve-other-service, ts-preserve-service]
    Вариант 2: [ts-delivery-service, ts-food-service]
InitData.java       × 9   →  9 вариантов тела   (разные seed-данные)
```

Для blast radius: изменение сигнатуры SecurityConfig затрагивает все 29. Изменение конкретного antMatcher URL — только сервисы с тем же `body_fp`.

### AST-скелет

Структурный скелет — только декларации, без тел методов, строковых литералов и комментариев:

```
Java:   @Annotation [модификаторы] class/method/field ИмяТипа Имя
Python: class/def ИмяФункции
Go:     func/type/var/const ИмяСимвола
JS/TS:  function/class/const/export ИмяСимвола
```

**Нормализация для `ast_exact`:** строки скелета сортируются и дедуплицируются перед хешированием.
Результат: `SecurityConfig.java` с методами в разном порядке → одинаковый `ast_exact`.

### Контекстный текст (уровень 4)

Извлекается из контента автоматически или передаётся явно (например, из `AnalysisResult`):

```
импорты + HTTP-URL из кода + аннотации Spring/Flask
```

Файлы, использующие одинаковые зависимости и вызывающие одинаковые сервисы, 
получают высокую схожесть по контексту даже при разных именах методов.

### Инвариант: «влияет только на него»

При вызове `index_file(path, new_content)`:
1. Вызывается `_evict(path)` — удаляются только записи этого файла из всех инвертированных индексов.
2. Записываются новые записи для этого файла.
3. Остальные файлы (включая бывших «клонов») не затрагиваются.

Следствие: если `SecurityConfig.java` в `ts-travel-service` изменился, а в `ts-order-service` — нет,
то `ts-order-service` продолжает иметь прежний `ast_exact` отпечаток.
Query по изменённому файлу вернёт `ts-order-service` на уровне `ast_vector` (не `ast_exact`),
отражая факт расхождения.

### Структуры данных

```python
# Инвертированные индексы (уровни 1 и 2)
_hash_index:  dict[sha256  → set[path]]
_fp_index:    dict[ast_fp  → set[path]]

# Векторные хранилища (уровни 3 и 4)
_ast_vecs:    dict[path → Counter[str]]   # n-грамм вектор скелета
_ctx_vecs:    dict[path → Counter[str]]   # n-грамм вектор контекста

# Обратный lookup для быстрого _evict()
_file_hashes: dict[path → sha256]
_file_fps:    dict[path → ast_fp]
```

---

## Постобработка результатов query()

Два механизма постобработки для offline-use без LLM:

### Knee cutoff

Динамический отсев хвоста вместо фиксированного порога:

```
Пример: [0.95×28, 0.72, 0.68, ...]
drop(0.95 → 0.72) = 0.23 > 0.15 × 0.95 = 0.143 → обрезаем с индекса 28
```

Параметры: `use_knee=True, knee_drop_ratio=0.15`

### MMR (Maximal Marginal Relevance)

Предотвращает возврат N одинаковых клонов. При запросе по `SecurityConfig.java`:

```
Без MMR (top_k=8): 8 × SecurityConfig из разных сервисов — бесполезно
С MMR  (λ=0.65):   1 × SecurityConfig + 7 структурно разнообразных файлов
```

Алгоритм: `MMR(d) = λ·similarity(d) − (1−λ)·max_sim(d, selected)`.
Разнообразие измеряется через AST-векторы (`_ast_vecs`).

Параметры: `use_mmr=True, mmr_lambda=0.65`

### Union-Find cluster()

Транзитивная кластеризация: если A≈B и B≈C (выше порога), то {A, B, C} — один кластер.
Пуск Union-Find по всем парам `_ast_vecs`. O(n²) попарных сравнений — допустимо до ~2K файлов.

```
clone_groups(AST_EXACT): SecurityConfig.java × 29  (только точные совпадения)
cluster(threshold=0.85): SecurityConfig.java × 39  (транзитивные связи)
```

---

## Результаты на train-ticket

```
Проиндексировано: 635 Java-файлов за 0.31с

Hash-клон групп:  0   ← нет побайтовых клонов (Java-файлы отличаются именами пакетов)
AST-клон групп:  31   (clone_groups, точные)
Transitive clusters: 62 за 1.10с (cluster, threshold=0.85)

Топ AST-клон групп:
  SecurityConfig.java × 29  (все основные сервисы)
  InitData.java       × 9
  Queues.java         × 5

Transitive кластеры:
  CallAnalysis.java         × 86  (old-docs)
  SecurityConfig.java       × 39  (+10 через транзитивность)
  AdminBasicInfoController  × 20
```

MMR-запрос по `SecurityConfig.java` (λ=0.65, top_k=8):
```
[ast_exact   ] 0.95  ts-admin-order-service   ← 1 представитель группы клонов
[ctx_vector  ] 0.83  old-docs/TWMConf         ← разнообразные по ctx
[ctx_vector  ] 0.79  ts-delivery-service/Queues
[ctx_vector  ] 0.75  ts-inside-payment-service/ExecutorConfig
...
```

---

## Ограничения и будущие улучшения

1. **Уровень 3/4: O(n) линейный скан** — для 10K+ файлов нужен HNSW-индекс (LanceDB, ADR-030).
   `cluster()`: O(n²) — для 2K+ файлов нужен ANN.

2. **n-граммы AST — regex-based** — не настоящий tree-sitter AST. Для точного структурного
   анализа можно заменить `_ast_skeleton()` на tree-sitter обход (ADR-001).

3. **Upgrade path до BGE-M3:** уровни 3 и 4 можно заменить на семантические векторы BGE-M3
   без изменения публичного API — только `_ast_vecs`/`_ctx_vecs` станут другого типа.
   MMR и knee cutoff работают с любыми векторами через `_cosine()`.

4. **История изменений** — намеренно не реализована.

---

## Местонахождение кода

```
src/similarity/
├── __init__.py   # экспорт SimilarityIndex, SimilarFile, SimilarityLevel
└── index.py      # реализация (self-contained, stdlib-only)
```

Модульные функции (переиспользуются независимо от класса):
```python
_knee_cutoff(results, drop_ratio=0.15) → list[SimilarFile]
_mmr_select(candidates, vecs, lam=0.65, k=10) → list[SimilarFile]
```

Публичный API:
```python
idx = SimilarityIndex(ast_threshold=0.70, ctx_threshold=0.55)
idx.index_file(path, content=None, context=None)          # добавить/обновить
idx.remove_file(path)                                      # удалить
idx.query(path, top_k=10,                                  # найти похожие
          use_mmr=True, mmr_lambda=0.65,
          use_knee=True, knee_drop_ratio=0.15)
idx.clone_groups(level) → list[list[str]]                  # точные группы
idx.cluster(threshold=None) → list[list[str]]              # транзитивные кластеры
idx.stats() → dict
```
