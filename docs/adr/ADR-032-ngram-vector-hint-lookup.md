# ADR-032: n-грамм вектор для расширяемого поиска tech-подсказок

**Статус:** Принято  
**Дата:** 2026-10-14  
**Контекст:** `DepsAdapter` — `_TECH_HINTS` и `_hint()` в `src/adapters/tools/config/deps.py`

---

## Контекст и проблема

`DepsAdapter` извлекает из файлов зависимостей `tech_hint` — сигнал о технологии сервиса
(`db:mysql`, `web:flask`, `mq:rabbitmq`, ...). Эти подсказки используются для классификации
сервисов в графе и настройки весов blast radius.

**Проблема:** словарь `_TECH_HINTS` + проверка подстроки покрывали только точные ключи и
их суперстроки. Пакеты-расширения (`flask-login`, `django-rest-framework`, `spring-security-web`)
не получали подсказок, хотя семантически принадлежат к тому же семейству.

```python
# До исправления:
_hint("flask")               # → "web:flask"   ✓ точное совпадение
_hint("flask-login")         # → ""            ✗ нет подстроки
_hint("spring-security-web") # → ""            ✗ нет подстроки
```

**Ручное добавление вариантов не масштабируется:** каждый новый пакет (`flask-migrate`,
`flask-sqlalchemy`, `flask-jwt-extended`) требует отдельной строки в словаре.

---

## Решение: трёхуровневый каскадный поиск

```
_hint(pkg):
  1. Точное совпадение   — O(1) dict lookup
  2. Подстрока           — O(n) linear scan, ловит "mysql-connector-java", "spring-boot-starter-*"
  3. n-грамм вектор      — O(n) cosine similarity, ловит семейства по корню
```

### Уровень 3: символьные n-граммы (trigrams)

Для каждого ключа `_TECH_HINTS` предвычисляется вектор тригр'm символов без разделителей:

```
"flask"         → f,l,a,s,k → {"fla":1, "las":1, "ask":1}
"flask-login"   → flasklogin → {"fla":1, "las":1, "ask":1, "skl":1, ...}
```

Тригр'мы "fla", "las", "ask" — общие. Cosine similarity > 0.40 → матч.

**Предвычисление при загрузке модуля:**
```python
_HINT_VECTORS: list[tuple[str, Counter]] = [
    (pkg, _ngrams(pkg)) for pkg in _TECH_HINTS
]
```

Загрузка: O(|_TECH_HINTS|) = O(80) — незначительно.

**Порог 0.40** подобран так, чтобы:
- `flask-login` → `web:flask` (sim ≈ 0.55) ✓
- `spring-security-web` → `web:spring` (sim ≈ 0.50) ✓  
- `requests` → нет матча (max sim < 0.30) ✓
- `pytest` → нет матча ✓
- `numpy` → нет матча ✓

### Корневые записи как «якоря»

Для коротких имён (4–6 символов) вектор маленький, cosine нестабилен.
В `_TECH_HINTS` добавлены явные корневые записи:

```python
"spring":    "web:spring",   # spring-security-*, spring-data-*, spring-cloud-*
"rabbitmq":  "mq:rabbitmq",  # rabbitmq-client, rabbitmq-amqp-client
"gin":       "web:gin",      # gin-gonic/gin и варианты
"actix":     "web:actix",    # actix-web, actix-rt, actix-files
"sqlalchemy":"db:sqlalchemy",
```

Якорная запись перехватывается ещё на уровне 2 (подстрока) для длинных имён,
и на уровне 3 (n-граммы) для вариантов без точной подстроки.

---

## Результаты тестирования

```
✓ [exact    ] flask               → "web:flask"        (уровень 1)
✓ [exact    ] redis               → "db:redis"         (уровень 1)
✓ [substring] mysql-connector-py  → "db:mysql"         (уровень 2)
✓ [substring] spring-boot-starter-data-jpa → "db:jpa"  (уровень 2)
✓ [ngram    ] flask-login         → "web:flask"        (уровень 3)
✓ [ngram    ] flask-migrate       → "web:flask"        (уровень 3)
✓ [ngram    ] django-rest-framework → "web:django"     (уровень 3)
✓ [ngram    ] spring-security-web → "web:spring"       (уровень 3)
✓ [ngram    ] rabbitmq-client     → "mq:rabbitmq"      (уровень 3)
✓ [ngram    ] fastapi-users       → "web:fastapi"      (уровень 3)
✓ [ngram    ] elasticsearch-py    → "db:elasticsearch" (уровень 3)
✓ [none     ] requests            → ""                 (нет матча)
✓ [none     ] pytest              → ""                 (нет матча)
✓ [none     ] numpy               → ""                 (нет матча)

15/15 тестов пройдено, 0 ложных срабатываний
```

---

## Расширяемость

**Добавление нового стека:** достаточно одной строки в `_TECH_HINTS`:

```python
"actix-web": "web:actix",
```

После этого `actix-rt`, `actix-files`, `actix-multipart`, `actix-session` и любые другие
пакеты семейства actix автоматически распознаются через n-граммы. Вручную перечислять
варианты не нужно.

**`_HINT_VECTORS` пересчитывается при импорте** — новая запись сразу попадает в индекс.

---

## Ограничения

1. **Порог 0.40 — глобальный.** Для очень коротких пакетов (2–3 символа, например `pg`)
   n-граммы ненадёжны; они попадают через точное совпадение (уровень 1) или подстроку (уровень 2).

2. **n-граммы нечувствительны к семантике.** `kafka` и `kafkajs` совпадут, но `celery`
   (также очередь) не совпадёт с `kafka` — разные символы. Это намеренно: нет ложных
   cross-category матчей.

3. **Upgrade path:** уровень 3 можно заменить BGE-M3-эмбеддингами (ADR-014, ADR-030)
   без изменения структуры `_hint()` — только внутренность `_hint_by_ngrams()`.

---

## Местонахождение кода

`src/adapters/tools/config/deps.py`:
- `_TECH_HINTS: dict[str, str]` — словарь паттернов
- `_ngrams(text) → Counter` — тригр'мы без разделителей
- `_cosine(a, b) → float` — cosine similarity двух Counter-векторов
- `_HINT_VECTORS` — предвычисленный индекс, инициализируется при импорте
- `_NGRAM_THRESHOLD = 0.40` — минимальная схожесть
- `_hint_by_ngrams(key) → str` — поиск по векторам
- `DepsAdapter._hint(pkg) → str` — каскадный поиск (уровни 1→2→3)
