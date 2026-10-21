# Tech Debt — аудит хардкода и хаков

> Принципы оценки: **переносимость** (код должен работать на любом репозитории,
> не только train-ticket), **расширяемость** (новый домен ≠ правка Python-кода),
> **single source of truth** (одно место хранения константы).
>
> Каждый пункт: текущий хак → правильное решение → трудозатраты.

---

## TD-1. Хардкод путей к skeleton/targets в predict.py

**Файл:** `src/predict.py:20-21`

```python
SKELETON = _ROOT / "tt-challenge" / "skeleton" / "trainticket.json.gz"
TARGETS  = _ROOT / "tt-challenge" / "targets" / "trainticket"
```

**Проблема:** при смене датасета нужно редактировать Python-файл.
CLI-аргументы `--skeleton` и `--targets` уже есть, но *значения по умолчанию* вшиты в код.

**Правильно:** читать из env vars с fallback к текущим путям:
```python
import os
SKELETON = Path(os.getenv("TT_SKELETON", str(_ROOT / "tt-challenge/skeleton/trainticket.json.gz")))
TARGETS  = Path(os.getenv("TT_TARGETS",  str(_ROOT / "tt-challenge/targets/trainticket")))
```
Тогда `docker run -e TT_SKELETON=/data/sk.json.gz ...` работает без изменения кода.

**Трудозатраты:** 10 мин. Риск: нулевой (CLI-аргументы остаются).

---

## TD-2. Хардкод пути к кешу в service_profiles.py и qwen_encoder.py

**Файлы:**
- `src/localization/service_profiles.py:55` → `Path(__file__).parents[2] / "cache" / "service_profiles_v1.pkl"`
- `src/localization/qwen_encoder.py:40` → `Path(__file__).parents[2] / "cache" / "qwen_encoder_cache.pkl"`

**Проблема:** `parents[2]` жёстко привязывает cache к структуре директорий.
При переименовании директории `src/localization/` → сломается.
`parents[N]` — это хрупкий способ подняться до корня.

**Правильно:** добавить `_config.py` (уже есть) геттер `cfg_cache_dir()`:
```python
# _config.py
def cfg_cache_dir() -> Path:
    return Path(os.getenv("TT_CACHE_DIR", Path(__file__).parent.parent / "cache"))
```
Все cache-пути: `cfg_cache_dir() / "service_profiles_v1.pkl"`.

**Трудозатраты:** 30 мин (3 файла).

---

## TD-3. Имена опциональных модулей вшиты в predict.py

**Файл:** `src/predict.py:170-175`

```python
_FOOD_MODS = frozenset({
    "ts-food-service", "ts-food-delivery-service",
    "ts-train-food-service", "ts-station-food-service",
})
_INS_MODS    = frozenset({"ts-assurance-service"})
_CONSIGN_MODS = frozenset({"ts-consign-service"})
```

**Проблема:** доменные имена сервисов в Python-коде.
Для другого репозитория — другие имена, нужно редактировать код.

**Правильно:** вынести в `domain_config.yaml`:
```yaml
optional_service_groups:
  food:    ["ts-food-service", "ts-food-delivery-service", "ts-train-food-service", "ts-station-food-service"]
  insurance: ["ts-assurance-service"]
  consign:   ["ts-consign-service"]
```
В predict.py читать через `cfg_get("optional_service_groups", {})`.

**Трудозатраты:** 20 мин. Side effect: та же структура нужна если появится новый опциональный сервис.

---

## TD-4. Дублирование паттернов train_type между oracle.py и domain_config.yaml

**Файлы:**
- `src/trace/oracle.py:53-59` — `_TRAIN_G`, `_TRAIN_D`, `_TRAIN_NOT_G` компилируются через `_build_re()` из конфига
- `src/domain_config.yaml:133-148` — `oracle_train_patterns` → единственный источник
- `src/localization/step_typer.py:43-49` — `_G_TRAIN`, `_OTHER_TRAIN` аналогично из `step_train_patterns`
- `src/domain_config.yaml:107-126` — `step_train_patterns`

**Текущий статус:** архитектурно правильно — оба читают из YAML через `cfg_get()`.
**Оставшийся хак:** захардкоженные `default_alts` в вызовах `_build_re("G", [...])` — дублируют YAML.
Если YAML удалят, код продолжит работать со старыми дефолтами незаметно.

**Правильно:** убрать дефолтные списки из кода, оставить только `default_alts=[]`:
```python
_TRAIN_G = _build_re("G", [])   # fail fast если конфига нет
```
Или явно документировать, что дефолты — это fallback при отсутствии конфига.

**Трудозатраты:** 15 мин (низкий риск — только убрать дублирование дефолтов).

---

## TD-5. Дублирование regex food/insurance/luggage в oracle.py и step_typer.py

**Файлы:**
- `src/trace/oracle.py:70-76` — `_HAS_FOOD`, `_NO_FOOD`, `_HAS_INS`, `_NO_INS`, `_HAS_LUG`, `_NO_LUG`
- `src/localization/step_typer.py:61-66` — те же паттерны, немного другие

**Проблема:** одни и те же семантические понятия описаны дважды, и они *расходятся*:
```python
# oracle.py
_HAS_FOOD = re.compile(r"\b(with\s+food|order\s+food|food\s+service|station\s+food|food\s+from\s+a\s+station)\b", ...)
# step_typer.py  
_HAS_FOOD = re.compile(r"\b(with\s+food|order\s+food|food\s+on\s+the\s+train|station\s+food)\b", ...)
```
При исправлении нужно помнить об обоих файлах — это источник скрытых багов.

**Правильно:** добавить `optional_service_patterns` в `domain_config.yaml`:
```yaml
optional_service_patterns:
  has_food:    ['with\s+food', 'order\s+food', 'food\s+service', 'station\s+food', 'food\s+from\s+a\s+station']
  no_food:     ['no\s+food', 'without\s+food']
  has_insurance: ['with\b.{0,80}\b(assurance|insurance)']
  no_insurance:  ['(without|no)\b.{0,80}\b(assurance|insurance)']
  has_luggage: ['with\s+luggage', 'luggage\s+service', 'a\s+luggage\s+consignment']
  no_luggage:  ['no\s+luggage', 'without\s+luggage']
```
Оба файла читают из `cfg_get("optional_service_patterns", {...})`.

**Трудозатраты:** 1 час. Риск: средний (нужно проверить eval после).

---

## TD-6. Мёртвые поля _HAS_FOOD_STATION, _HAS_FOOD_TRAIN в oracle.py

**Файл:** `src/trace/oracle.py:63-68, 93-94`

```python
_HAS_FOOD_STATION = re.compile(...)   # определён, НЕ ИСПОЛЬЗУЕТСЯ
_HAS_FOOD_TRAIN   = re.compile(...)   # определён, НЕ ИСПОЛЬЗУЕТСЯ

@dataclass
class Oracle:
    has_food_train:   bool = False   # поле не заполняется
    has_food_station: bool = False   # поле не заполняется
```

**Проблема:** остатки откатившегося эксперимента Г7 (station food vs train food).
Загрязняют код, вводят читателя в заблуждение.

**Правильно:** удалить `_HAS_FOOD_STATION`, `_HAS_FOOD_TRAIN`, поля `has_food_train` и `has_food_station` из `Oracle`.

**Трудозатраты:** 10 мин. Риск: нулевой (нигде не используются).

---

## TD-7. _NOT_G_TRAIN в step_typer.py — не из конфига

**Файл:** `src/localization/step_typer.py:53-56`

```python
_NOT_G_TRAIN = re.compile(
    r"(not\s+high.?speed|regular\s+train|other\b.{0,30}\btype)",
    re.I,
)
```

**Проблема:** в отличие от `_G_TRAIN` и `_OTHER_TRAIN`, этот паттерн не читается из конфига,
хотя `oracle.py` тот же NOT_G берёт из `oracle_train_patterns.NOT_G`.

**Правильно:** добавить `step_train_patterns.NOT_G` в YAML и читать через `_build_train_re("NOT_G", [...])`.

**Трудозатраты:** 10 мин.

---

## TD-8. Хардкод ollama/HF модели в qwen_encoder.py

**Файл:** `src/localization/qwen_encoder.py:34-36`

```python
_OLLAMA_GENERATE_URL = "http://localhost:11434/api/generate"
_DEFAULT_OLLAMA_MODEL = "qwen2.5-coder:3b"
_HF_MODEL_NAME       = "Qwen/Qwen2.5-Coder-1.5B-Instruct"
```

**Проблема:** при смене модели нужно редактировать Python-код.
Особенно URL — если ollama запускается не на localhost, или другой порт.

**Правильно:**
```python
_OLLAMA_BASE_URL    = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
_DEFAULT_OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:3b")
_HF_MODEL_NAME      = os.getenv("HF_QWEN_MODEL", "Qwen/Qwen2.5-Coder-1.5B-Instruct")
```

**Трудозатраты:** 15 мин. Риск: нулевой (только добавляем env-var override).

---

## TD-9. Удаление ts-/−service суффиксов в predict.py _write_viz

**Файл:** `src/predict.py:347`

```python
{"id": s, "label": s.replace("ts-", "").replace("-service", "")}
```

**Проблема:** `service_naming.prefix` и `.suffix` уже определены в `domain_config.yaml`,
но здесь они продублированы строками.

**Правильно:**
```python
from src._config import cfg_get
_prefix = cfg_get("service_naming", {}).get("prefix", "")
_suffix = cfg_get("service_naming", {}).get("suffix", "")
# ...
{"id": s, "label": s.removeprefix(_prefix).removesuffix(_suffix)}
```

**Трудозатраты:** 10 мин. Заодно починит отображение для других репозиториев с другими префиксами.

---

## TD-10. ✅ _safe_keyword_types вшит в step_typer.py вместо конфига

**Файл:** `src/localization/step_typer.py:21-25`

```python
_SAFE_KEYWORD_TYPES = frozenset({
    "book", "pay", "cancel", "collect", "rebook",
    "enter_station", "add_consign", "query_consign",
    "check_price", "check_refund",
})
```

**Проблема:** при добавлении нового step_type нужно помнить обновить этот frozenset в коде.
Семантически это конфигурация ("для каких типов шагов Qwen-keywords безопасны"), а не логика.

**Правильно:** вынести в `domain_config.yaml`:
```yaml
safe_keyword_step_types:
  - book
  - pay
  - cancel
  # ...
```

**Трудозатраты:** 20 мин.

---

## TD-11. ✅ JWT-фильтр ищется по `.jwt.` в fqn — неявный контракт

**Файл:** `src/trace/closure.py:56`

```python
if ".jwt." in fqn or ".security.jwt." in fqn:
```

**Проблема:** правило "Spring Security фильтр → пакет содержит `.jwt.`" специфично для train-ticket.
В другом проекте Spring Security фильтры могут быть в других пакетах.

**Правильно:** добавить в `domain_config.yaml`:
```yaml
security_filter_patterns:
  package_contains: ['.jwt.', '.security.jwt.']
  method_starts: ['doFilter']
```
Читать через `cfg_get("security_filter_patterns", {...})`.

**Трудозатраты:** 30 мин. Приоритет: низкий (работает для train-ticket, замена чисто для переносимости).

---

## TD-12. `parents[N]` для поиска корня проекта в closure.py и searcher.py

**Файлы:**
- `src/trace/closure.py:17` → `_HERE = Path(__file__).parent.parent.parent`
- `src/localization/searcher.py:18` → `_HERE = Path(__file__).parent.parent.parent`

Оба затем делают `sys.path.insert(0, str(_HERE))`.

**Проблема:** если структура директорий изменится (переместить файл на уровень вглубь/вверх), путь сломается молча.

**Правильно:** использовать `importlib.util.find_spec` или `src._config` уже знает корень:
```python
# В _config.py уже есть _ROOT
from src._config import _ROOT
sys.path.insert(0, str(_ROOT))
```
Или `pyproject.toml` с `[tool.setuptools]` позволяет убрать `sys.path.insert` вовсе.

**Трудозатраты:** 20 мин.

---

## TD-13. ✅ Версия в имени cache-файла: `service_profiles_v1.pkl`

**Файл:** `src/localization/service_profiles.py:55`

```python
cache_path = str(Path(...) / "cache" / "service_profiles_v1.pkl")
```

**Проблема:** при изменении схемы `ServiceProfile` нужно:
1. Поднять версию в имени файла
2. Обновить путь в Python-коде
3. Не забыть про pack.py `CACHE_FILES`

**Правильно:** имя файла через `_CACHE_KEY_VERSION` (уже есть в коде!):
```python
_CACHE_KEY_VERSION = "sp_v1"   # уже определено
cache_path = str(cfg_cache_dir() / f"service_profiles_{_CACHE_KEY_VERSION}.pkl")
```
pack.py тогда ищет `cache/service_profiles_*.pkl` вместо жёсткого имени.

**Трудозатраты:** 15 мин (TD-2 и TD-13 решаются вместе).

---

## TD-14. Backward compat patch в load_profiles() — "старые pickled объекты не имеют tags"

**Файл:** `src/localization/service_profiles.py:67-70`

```python
if not hasattr(item, "tags"):
    item.tags = []
```

**Проблема:** шим совместимости со старыми .pkl будет жить вечно, если не удалить старый кеш.
При росте схемы это превращается в минное поле.

**Правильно:** при бампе версии (TD-13) удалять кеш старой версии и убирать шим.
Или перейти на `dataclasses.asdict` → JSON → читать через `dacite.from_dict` с `strict=False`.

**Трудозатраты:** 30 мин.

---

## TD-15. Хардкод `system_date` и `trip_date` в Oracle

**Файл:** `src/trace/oracle.py:87-88`

```python
system_date: date = field(default_factory=lambda: date(2026, 9, 15))
trip_date:   date = field(default_factory=lambda: date(2026, 10, 1))
```

**Проблема:** конкретные даты из конкурса вшиты в код.
Не влияет на текущий score, но портит переносимость.

**Правильно:** читать из конфига или принимать как параметр `Oracle(system_date=...)`.
Или просто убрать значения по умолчанию — они не используются в логике pruning.

**Трудозатраты:** 5 мин. Приоритет: низкий.

---

## TD-16. `account: str = "fdse_microservice"` в Oracle

**Файл:** `src/trace/oracle.py:85`

```python
account: str = "fdse_microservice"
```

**Проблема:** конкретное имя аккаунта из train-ticket hardcoded в коде.
Не влияет на score (поле не используется в текущей логике).

**Правильно:** убрать из дефолта или вынести в конфиг.

**Трудозатраты:** 5 мин. Приоритет: очень низкий.

---

## TD-17. `.jwt.` / `.security.jwt.` — паттерн Spring специфичен

Частично описан в TD-11, но есть более глубокая проблема:
`doFilter` — это интерфейс `javax.servlet.Filter`. В Spring Boot 3+ пакет сменился на
`jakarta.servlet.Filter`. Если конкурс обновит проект — паттерн отломается.

**Правильно:** дополнительно проверять `implements_raw` для `javax.servlet.Filter` / `jakarta.servlet.Filter`.
Скелет уже хранит `implements_raw` → авто-обнаружение без `fqn`-хака.

**Трудозатраты:** 1 час. Приоритет: средний (защита от upgrade Jakarta EE).

---

## Сводная таблица

| # | Файл | Проблема | Тип | Трудозатраты | Приоритет |
|---|------|----------|-----|-------------|-----------|
| TD-1 | predict.py | Пути skeleton/targets хардкод | Путь | 10 мин | Высокий |
| TD-2 | service_profiles.py, qwen_encoder.py | `parents[N]` для cache | Путь | 30 мин | Высокий |
| TD-3 | predict.py | Имена опциональных сервисов | Домен | 20 мин | Высокий |
| TD-4 | oracle.py, step_typer.py | Дефолты дублируют YAML | Конфиг | 15 мин | Средний |
| TD-5 | oracle.py, step_typer.py | Дублирование food/ins/lug regex | Конфиг | 1 час | Средний |
| TD-6 | oracle.py | Мёртвые поля Г7 | Мусор | 10 мин | Высокий |
| TD-7 | step_typer.py | NOT_G не из конфига | Конфиг | 10 мин | Низкий |
| TD-8 | qwen_encoder.py | Ollama URL/модель хардкод | Путь/конфиг | 15 мин | Средний |
| TD-9 | predict.py | ts-/-service strip повторно | Конфиг | 10 мин | Низкий |
| TD-10 ✅ | step_typer.py | safe_keyword_types вшит в код | Конфиг | 20 мин | Низкий |
| TD-11 ✅ | closure.py | JWT-пакет `.jwt.` хардкод | Домен | 30 мин | Низкий |
| TD-12 ✅ | closure.py, searcher.py | `parents[3]` для sys.path | Путь | 20 мин | Средний |
| TD-13 ✅ | service_profiles.py | Версия pkl в имени файла | Конфиг | 15 мин | Средний |
| TD-14 | service_profiles.py | Backward compat hasattr shim | Мусор | 30 мин | Низкий |
| TD-15 | oracle.py | Конкретные даты 2026 | Домен | 5 мин | Низкий |
| TD-16 | oracle.py | `account = "fdse_microservice"` | Домен | 5 мин | Очень низкий |
| TD-17 | closure.py | javax vs jakarta Filter | Домен | 1 час | Средний |

**Быстрые победы (< 30 мин суммарно):** TD-6 + TD-15 + TD-16 — удаление мусора без риска.  
**Наибольший прирост переносимости:** TD-1 + TD-2 + TD-3 — вынести пути и имена сервисов в env/конфиг.  
**Наибольший вклад в расширяемость:** TD-5 — единый источник правды для всех optional-паттернов.
