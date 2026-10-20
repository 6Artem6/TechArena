# Применимость Qwen и LLM-препроцессинга в пайплайне

**Дата:** 2026-10-18 (обновлено)  
**Контекст:** TechArena — Execution Trace Prediction, Train Ticket microservices  
**Score на момент написания:** 0.7319 → **актуальный best: 0.7683** (S1=0.807, S2=0.737, S3=0.802, S4=0.808, S5=0.741, S6=0.801, S7=0.683). Актуальный статус — в IMPROVEMENTS.md.

---

## Итог экспериментов с sibling-фильтрацией

### Проблема S5=0.594
`getTransferSearch` вызывает оба сервиса `ts-travel-service` (G-side) и `ts-travel2-service` (D-side) параллельно. Sibling-фильтрация в `pruned_closure` блокирует G-side при `train_type="D"`, что неверно для transfer-поиска (он охватывает все типы поездов).

### Попытка 1 — глобальное снятие фильтра
**Результат:** регрессия S2 −0.082, S7 −0.080, средний 0.7254 → 0.6966.  
Причина: пара `ts-order-service / ts-order-other-service` фильтруется правильно — в GT каждого сценария только одна из сторон (G или D), но без фильтра оба попадают в prediction.

### Попытка 2 — паттерн «оба сиблинга из одного модуля»
**Результат:** идентичная регрессия.  
Причина: `ts-inside-payment-service`, `ts-seat-service`, `ts-execute-service` тоже вызывают оба order-сервиса из одного модуля → паттерн срабатывает повсеместно → эффект равен попытке 1.

### Вывод
Sibling-фильтрация оставлена на месте. S5=0.594 — известное ограничение текущей архитектуры. Для его решения нужен step_type-aware bypass или улучшение локализации handler-ов.

---

## Оценка предложенных идей

### 1. Qwen 1.5B → 3B (сделано)
**Статус:** ✅ Реализовано  
**Файл:** `src/localization/qwen_encoder.py` — `_DEFAULT_OLLAMA_MODEL = "qwen2.5-coder:3b"`  
**Ожидаемый эффект:** улучшение качества терминологических инжекций при поиске handler-ов  
**Риск:** медленнее в 2× при холодном старте (ollama кэширует после первого запроса)

---

### 2. Локальное хранение BGE-M3 (не сделано)
**Приоритет:** Средний  
**Суть:** `snapshot_download("BAAI/bge-m3", local_dir="models/bge-m3")` + `FlagEmbedding(model_name_or_path="models/bge-m3")`  
**Эффект:** устранение сетевой зависимости при запуске; время первого токена снизится  
**Реализация:** 15 минут — добавить в `install.sh` и поменять путь в `searcher.py`

---

### 3. Паспорта handler-ов (→ SP.1/SP.2/SP.3) ✅
**Статус:** ✅ Реализовано. Сделано иначе, чем планировалось: не LanceDB, а `cache/service_passports.pkl` (240 паспортов). Каждый паспорт: `business_summary` + `synonyms[]`. Синонимы добавлены в doc_texts для CE и BGE-M3.

**Что работает:** getTransferResult теперь отличим от getByCheapest в ts-travel-plan-service. S5: 0.640 → 0.741 → 0.801.

**Что НЕ реализовано из оригинального плана:** ANN-поиск через LanceDB (нет необходимости при 240 handlers — BM25+BGE справляются).

---

### 4. Обогащение запроса через данные сервиса (частично реализовано)
**Статус:** Частично — Qwen уже инжектирует термины в запрос (≤6 char фильтр)  
**Проблема:** текущий 6-символьный фильтр блокирует валидные короткие термины  
**Улучшение:** заменить лексический фильтр на семантический (косинусное сходство с запросом ≥0.3)  
**Файл:** `src/localization/qwen_encoder.py` — метод инжекции терминов

**Расширенная идея:** после быстрого ANN-поиска (кандидаты сервисов) передавать Qwen:
- Запрос пользователя
- Имена методов топ-3 сервисов-кандидатов
- Граф data-flow между кандидатами
→ Qwen возвращает уточнённый запрос с правильной терминологией домена

---

### 5. Синонимы к запросу из описаний сервисов
**Приоритет:** Средний  
**Суть:** вместо обогащения запроса терминами → искать в описаниях сервисов синонимы к словам из запроса → добавлять их как расширение  
**Пример:** `book ticket` → найти методы с `reserve`, `create order`, `purchase` → расширить запрос  
**Реализация:** BM25 по описаниям сервисов → extract ключевых слов → добавить в запрос  
**Риск:** шум если описания плохого качества

---

### 6. Граф data-flow как словарь связей
**Приоритет:** Средний  
**Суть:** предвычислить из `wiring["remote_call"]` словарь `{сервис: [сервисы, которые он вызывает]}` → при поиске handler-а учитывать, что если X найден → его remote_call-соседи тоже кандидаты  
**Отличие от текущего BFS:** сейчас BFS стартует от handler-а и идёт вглубь. Идея — использовать граф для pre-filtering кандидатов перед BFS.  
**Файл:** уже частично есть в `oracle.py::record_step()` — `reachable_modules`

---

### 7. LanceDB для кластеризации ключевых слов
**Приоритет:** Низкий  
**Суть:** Qwen анализирует сходства между именами методов → кластеризует → формирует семантические группы (например, "payment methods", "search methods")  
**Проблема:** кластеры нужно поддерживать актуальными при изменении кодовой базы  
**Риск:** сложность реализации высокая, эффект неочевиден

---

### 8. Qwen-генерация правил из AST
**Приоритет:** Низкий  
**Суть:** Qwen анализирует код методов → формулирует правила классификации (шаг = "book" если метод вызывает `createOrder`)  
**Проблема:** это фактически хардкод через LLM — нарушает Zero-Hardcode Manifesto если правила жёстко зафиксировать  
**Допустимый вариант:** Qwen генерирует не правила, а описания методов → хранятся в LanceDB → используются для семантического поиска (→ идея 3)

---

## Приоритетный роадмап (обновлено 2026-10-19)

**Текущий best: 0.768330** (S1=0.807, S2=0.737, S3=0.802, S4=0.808, S5=0.741, S6=0.801, S7=0.683).  
**Последний сабмит (регрессия): 0.755640** — изменения сессии 2026-10-19 дали −0.01269.

| Приоритет | Идея | Статус | Направление эффекта | Сложность |
|-----------|------|--------|---------------------|-----------|
| 1 | Паспорта handler-ов offline (SP.1/SP.2/SP.3) | ✅ Реализовано (pkl, 240 passports) | ↑ S5: 0.640→0.741→0.801 | — |
| 2 | StepParams contract P1.3 (services/payment/keywords) | ✅ Реализовано (консервативная версия) | ↑ S2: +0.014 | — |
| 3 | Персистентный Qwen-кеш | ✅ Реализовано | ↓ runtime | — |
| 4 | Локальное хранение BGE-M3 | ✅ Уже готово | стабильность запуска | — |
| **5** | **Расследование регрессии train_type PRIMARY** | **КРИТИЧНО** | **восстановление 0.768330** | **Низкая (~5 строк)** |
| **6** | **hub_scorer: добавить rerank_by_hub вызов** | **Не подключено** | **↑ ~0.005–0.01 avg** | **Низкая (~5 строк)** |
| **7** | **StepParams: search_criteria → extra_tokens** | **Не подключено** | **↑ ~0.005 S5** | **Низкая (~8 строк)** |
| **8** | **Bugfix get_service("luggage") startswith** | **Баг** | **↑ ~0.002 luggage steps** | **1 строка** |
| 9 | order_filter + actor_role подключение | Не подключено | ↑ ~0.003–0.005 | Низкая (~10 строк) |
| 10 | IDF-weighted BM25 func_variant disambiguation | ✅ Реализовано (сессия 2026-10-19) | S5 exec 208→214 ✓ | — |
| 11 | Semantic filter для Qwen keywords (вместо _SAFE_KEYWORD_TYPES) | Не реализовано | ↑ injection quality | Средняя |
| 12 | P-54 CFG branch pruning (tree-sitter if_statement → cfg_condition) | Не реализовано | ↑ ~0.02–0.05 (CRITICAL P-54) | Высокая (~70 строк) |
| 13 | SV.1 Bottom-up service validation (γ=0.10–0.20) | ✅→ОТКАТАНО | регрессия при текущих γ | — |

> **Примечание о количественных оценках:** IoU = |A∩B|/|A∪B| нелинейна.
> Реальный прирост зависит от текущих |TP|, |FP|, |FN| по каждому типу рёбер
> в каждом сценарии — без этих данных конкретные числа были бы выдумкой.
> Числовой эффект можно оценить только после реализации через eval на S0 и сабмит.

---

## Изменения сессии 2026-10-18 (часть 2)

### Index-based Qwen selection (реализовано)
`qwen_encoder.py`: вместо генерации имён handler-ов Qwen получает пронумерованный список кандидатов и возвращает `{index, confidence}`.

**Исправленные проблемы:**
- Баг `_init_backend`: `qwen2.5-coder:3b` не существует в ollama — все вызовы молча падали с HTTP 404. Исправлена логика определения модели: теперь точное совпадение, fallback по семейству (7b→1.5b по убыванию).
- `_NUM_PREDICT`: снижен с 160 до 40 токенов (индекс = 1-3 цифры).
- Lexical filter добавлен обратно: хотя бы одно слово ≥6 букв из имени handler-а должно присутствовать в action-тексте. Предотвращает галлюцинации (`home (conf=1.00)`, `createDefaultUser (conf=0.90)`).
- Attribution logging: `[qwen] module prev_handler → new_handler (conf=X.XX)` в stderr.
- Персистентный кеш: `~/.cache/qwen_encoder_cache.pkl`. Ускоряет повторные запуски (все запросы тех же шагов = 0 вызовов ollama).

**Текущий результат:** Qwen работает (7b), но lexical filter блокирует все инжекции текущих сценариев S0-S7. Qwen не улучшает и не ухудшает score. Кеш строится на первом прогоне.

### Предложенные направления (2026-10-18 session 2)

Пользователь предложил 3-фазную архитектуру интеграции Qwen:

**Фаза 1 (Offline LanceDB)** — наивысший приоритет:
- Qwen генерирует "бизнес-паспорта" для каждого Java-метода (1-2 предложения)
- BGE-M3 кодирует → LanceDB хранит
- Runtime ANN-поиск по action → топ-8 сервисов-кандидатов
- Эффект: устраняет разрыв словаря (consignment↔consign, board↔execute)

**Распараллеливание Qwen:**
- ollama + `OLLAMA_NUM_PARALLEL=4` на GPU
- Python ThreadPoolExecutor для параллельных HTTP-запросов
- Ограничение: oracle.step_history строится последовательно — нужен 2-проходный предиктор

**Персистентный кеш** — реализовано. Кеш по `(action, history, handlers)` → `{index, confidence}`.

---

## Изменения сессии 2026-10-18

### S5 split-closure (реализовано)
`predict.py`: при наличии `search_transit` шагов в сценарии entry-handler-ы этих шагов запускаются в отдельном `pruned_closure` без `train_type`-фильтра. Остальные entries используют штатный фильтр.

**Результат:** ts-travel-service теперь появляется в S5 remote (ранее 0 рёбер). Другие сценарии (S1-S4, S6-S7) не затронуты — у них нет `search_transit` шагов.

**Проверено на S0:** 0.980 → без регрессии.

### Extra_tokens для консигнации и check_refund (реализовано, без подтверждённого эффекта)
`step_typer.py`:
- `update_consign`, `add_consign`: добавлен "consign"×2 — мост между "consignment" в тексте и "consign" в именах handler-ов
- `check_refund`: добавлен "calculate", "refund" — усиление ts-cancel-service/calculate
- `query_consign`: НЕ изменяли — добавление "consign" ломает S0 (query_consign есть в S0)

**Проверено:** S7 предсказания идентичны до/после → BGE-M3 уже справляется с семантикой.

### BGE-M3 локальный кеш (уже готово)
Модели `BAAI/bge-m3` и `BAAI/bge-reranker-base` уже в `~/.cache/huggingface/hub/`. Дополнительных действий не требуется.

### Диагностика S7=0.634
- S7 имеет 49 remote edges (vs baseline 5) — значительно больше ожидаемого GT
- Основные источники over-prediction: ts-order-service:17 (из множества сервисов имеющих remote_call к нему), ts-food-service:1 (FP — S7 без food step)
- Root cause: BFS следует всем статическим remote_call рёбрам скелета, включая опциональные пути (food при бронировании)
- Без GT для S7 точечное исправление невозможно без риска регрессий

## Нерешённые проблемы

### S5=0.594 — Transfer search
**Причина:** sibling-фильтрация блокирует `ts-travel-service` для D-train.  
**Варианты решения:**
- Передавать `step_type` в `pruned_closure` → bypass фильтра для `search_transit`
- Улучшить локализацию — найти оба handler-а для `getTransferSearch`
- Описания сервисов (идея 3) → оба travel-сервиса попадут в кандидаты

### S7=0.634 — Неизвестный сценарий
**Причина:** не диагностирована — GT для S7 недоступен локально.  
**Гипотеза:** проблема с локализацией handler-ов или неверный train_type.

---

## Изменения сессии 2026-10-18 (часть 3 — StepParams, P-54)

### StepParams — три неиспользованных поля из PROBLEMS.md

Qwen уже извлекает 9 полей и кеширует. Три поля ни разу не были использованы в pipeline:

| Поле | Что Qwen извлекает (из кеша 69 cls_v2 записей) | Применение |
|---|---|---|
| `search_criteria` | `"quickest"`, `"fewest_stops"`, `"cheapest"`, `"transfer"` | BM25 extra_tokens для search_advanced — различают getByQuickest/getByCheapest/getByMinStation |
| `order_filter` | `"status:Paid"`, `"status:New"`, `"date:purchase"` | "paid" BM25 token для query_orders |
| `actor_role` | `"admin"`, `"passenger"` | is_admin hint в searcher.py → потенциально S7 |

**Баг:** `get_service("luggage")` — строгое равенство, Qwen возвращает `"luggage/consign"`. Luggage через Qwen не работает. Fix: `svc.name.startswith(name)`.

### P-54 из PROBLEMS.md — главный нереализованный Qwen use case

PROBLEMS.md помечает P-54 как CRITICAL: branch selection — главный bottleneck. `if (tripId.startsWith("G"))` → оба пути в BFS → FP remote/calls рёбра.

**Qwen offline применение:** анализировать Java if-statements в исходниках → извлекать условия вида `startsWith("G"/"D")` → сохранять как `cfg_condition` атрибут на remote_call рёбра → использовать в `pruned_closure` для фильтрации.

```python
# Пример: ts-preserve-service/preserveService.java
# if (tripId.startsWith("G") || tripId.startsWith("D")) → travel-service URL
# else → travel2-service URL
# cfg_condition = {"G": ts-travel-service, "OTHER": ts-travel2-service}
```

Инфраструктура уже есть: `src/parsers/java.py` запускает tree-sitter на 401 Java-файле. Нужно добавить traversal `if_statement` узлов → ~70 строк. Ожидаемый impact по P-54: даже с правильными entry points, без CFG IoU = 0.52–0.76.

**Почему Qwen здесь:** хотя это AST-задача, Qwen может дополнить tree-sitter для сложных условий (вложенные if, переменные-псевдонимы через alias analysis). Сначала реализовать через tree-sitter (P-54 §Шаг 1–3), Qwen как fallback для edge cases.

---

---

## Изменения сессии 2026-10-19

### IDF-weighted BM25 для func_variant disambiguation (новое)

**Проблема (было):** `_disambiguate_func_variant` использовал tag-overlap для решения ts-travel-service vs ts-travel-plan-service. Теги ts-travel-plan-service слишком общие (`travel-planning`, `train-routes`) → "search_transit" шаги шли в basic вместо plan.

**Решение (текущее):** IDF-weighted BM25 по `(vector_search_phrases + bm25_keywords)` всех профилей.
- IDF вычисляется один раз по корпусу 42 сервисов: `log((N - df + 0.5) / (df + 0.5) + 1)`
- Редкие термины с высоким IDF: "cheapest" (df=2, IDF=2.845), "transfer" (df=1, IDF=3.356), "fewest" (df=2, IDF=2.845)
- Правило: `want_plan` только когда `p_score > 0 AND p_score >= b_score`
- **Результат:** S5 exec восстановлен 208→214, calls 289→299 ✓

**Статус Qwen 1.5b тегов:** `_TAG_PLAN_TMPL` добавлен для plan-сервисов, но:
- ts-route-plan tags: `['transit', 'transfer', 'connecting', 'multi-leg', 'advanced', 'cheapest']` ✓ (хорошие)
- ts-travel-plan tags: `['train', 'route', 'station', 'journey', 'travel', 'plan']` ✗ (слишком общие)
- Qwen 1.5b игнорирует примеры в промпте и возвращает технические термины вместо user vocabulary
- **Теги не используются в текущем IDF-подходе** — заменены vector_search_phrases + bm25_keywords

### Qwen PRIMARY для train_type (изменение P1.3 conservative)

**Было (P1.3 conservative):** step_type и train_type — ВСЕГДА из regex; Qwen только для services/payment/keywords.

**Стало:** Qwen PRIMARY для train_type (`if step_params.train_type in ("G", "OTHER")`), regex как fallback.

**Риск:** Qwen кеш мог зафиксировать значения из предыдущих контекстов или использовать иной формат. Для шагов "Book ticket", "Pay for ticket" — Qwen может вернуть `train_type="G"` неявно, тогда как regex возвращал "" (без изменения oracle state).

**Подозрение:** именно это является главной причиной регрессии S4 0.808→0.776.

---

## Ограничения Zero-Hardcode Manifesto

Допускается:
- NLP/ML модели (BGE-M3, Qwen, BM25)
- Топологический анализ графа
- Сопоставление типовых сигнатур
- Статические рёбра skeleton-а
- AST-анализ

Запрещено:
- Regex-классификация шагов в коде предиктора
- YAML-конфиги с domain-правилами для handler-ов
- Domain-словари (hardcoded)
- Ручные state-флаги
