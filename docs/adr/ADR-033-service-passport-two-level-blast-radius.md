# ADR-033: ServicePassport — двухуровневый blast radius через паспорта сервисов

**Статус:** Принято  
**Дата:** 2026-10-14  
**Контекст:** Blast Radius Engine, слой агрегации между адаптерами и графовым движком

---

## Контекст и проблема

Адаптеры (YamlAdapter, DepsAdapter, CtagsAdapter, …) возвращают `list[Finding]` — плоский список фактов
уровня файлов. Граф blast radius, напротив, оперирует на уровне сервисов: «если изменился ts-train-service,
какие сервисы затронуты?».

**Проблема:** между файловыми Finding'ами и сервисным запросом нет промежуточного слоя.
Наивное решение — собрать все Finding'и репозитория в один граф и обходить его — не масштабируется:
в train-ticket ~2500 файлов × ~50 адаптеров = миллионы потенциальных рёбер.

**Требование:** O(n) первый уровень фильтрации, где n ≈ 68 (кол-во сервисов), без чтения Java-файлов.

---

## Рассмотренные варианты

| Вариант | Плюсы | Минусы |
|---|---|---|
| Прямой обход всего графа | Точно | O(E) по всем рёбрам, десятки тысяч рёбер |
| Кеш «файл → сервис» | Быстро, просто | Не даёт семантику (tech_stack, endpoints, …) |
| **ServicePassport + PassportRegistry** | Семантика + O(n) coarse | Неполнота без Java adapter |
| Граф зависимостей pom.xml | Точные Maven-зависимости | Нет runtime вызовов |

---

## Решение

**MAP → REDUCE** двухфазный pipeline поверх существующих адаптеров:

```
MAP    = адаптеры на файлах → list[Finding]         (уже есть)
REDUCE Phase 1 = ServicePassport.from_findings()    (per-service агрегация)
REDUCE Phase 2 = PassportRegistry.build_call_graph() (cross-service граф)
```

### ServicePassport — структура

```python
@dataclass
class ServicePassport:
    name: str                    # "ts-travel-service"
    tech_stack: list[str]        # ["db:jpa", "db:mysql", "web:spring"]  ← из IMPORT findings
    endpoints: list[dict]        # [{path, method}]                       ← из HTTP_ENDPOINT
    calls_out: list[str]         # ["ts-train-service", ...]              ← из CALLS_SERVICE
    calls_in: list[str]          # заполняет PassportRegistry
    db_deps: list[str]           # ["mysql:ts-travel-mysql:3306"]         ← из CALLS_DB
    mq_deps: list[str]           # ["amqp:rabbitmq:5672"]                 ← из ASYNC_CALL
    ports: list[int]             # [12346]                                ← из SERVICE_DEF
    env_keys: list[str]          # ["TRAIN_SERVICE_HOST", ...]            ← из ENV_ACCESS
    proxy_routes: list[dict]     # nginx маршруты                         ← из PROXY_ROUTE
    findings_count: int          # кол-во исходных Finding'ов
```

### PassportRegistry — двухуровневый blast radius

**Level-1 (coarse BFS):** сканирует ~68 паспортов, `O(n · depth)`, без чтения файлов.

```python
registry.affected_by("ts-basic-service", depth=3)
# → {"ts-travel-service": 1, "ts-food-service": 2}
# Результат: 2 кандидата из 39 для углублённого анализа
```

**Level-2 (fine drill):** запускает файловые адаптеры только для кандидатов из Level-1.
Загрузка файлов: 2/39 = 5% вместо 100%.

---

## Текстовое представление для n-граммной схожести

`to_text()` генерирует нормализованный текст для косинусного сравнения:

```
"svc:ts-travel-service tech:db:jpa tech:db:mysql calls:ts-basic-service calls:ts-route-service db:ts-travel-mysql port:12346"
```

Правило нормализации: path params `{orderId}` → `{p}` для обобщения.

`similar_services()` использует n-граммные векторы паспортов для обнаружения сервисов со схожей
ролью (аналог `SimilarityIndex.cluster()` для файлов, ADR-031):

```
similar_services("ts-travel-service", top_k=5):
  0.85  ts-food-service       ← тоже хаб с calls_out + db:mysql
  0.81  ts-security-service
  0.60  ts-train-service
```

---

## Результаты на train-ticket (39 сервисов)

```
Время построения: 0.08с (YAML + pom.xml)

stats:
  services:             39
  with_incoming_calls:  10
  leaf_services:        29
  total_call_edges:     11   ← только из application.yml; без Java AST

Топ хабов по calls_out:
  ts-travel-service    → 5 calls
  ts-food-service      → 4 calls
  ts-security-service  → 2 calls

Топ по calls_in (популярные):
  ts-order-service     ← 2 callers
  ts-basic-service     ← 1 caller
  ts-route-service     ← 1 caller
```

BFS примеры:
```
affected_by("ts-basic-service", depth=3):
  hop=1  ts-travel-service
  hop=2  ts-food-service

affected_by("ts-train-service", depth=2):
  hop=1  ts-travel-service
  hop=2  ts-food-service
```

---

## Ограничения (текущие)

### Неполнота вызовов без Java adapter

`total_call_edges: 11` — только вызовы из `application.yml` (Spring service URLs).
Основная масса вызовов в train-ticket — Java RestTemplate/WebClient — не попадает в паспорт
до реализации Java AST adapter (ADR-001).

**Следствие:** Level-1 BFS сейчас показывает нижнюю оценку affected set.
Реальные blast radius кандидаты могут быть шире.

### `total_endpoints: 0`

HTTP endpoints (`@GetMapping`, `@PostMapping`) извлекаются только из Java-файлов.
Поле `passport.endpoints` остаётся пустым до интеграции CtagsAdapter / Java AST adapter.

### O(n²) similar_services

Полный перебор всех паспортов. При >500 сервисах потребуется HNSW (LanceDB, ADR-030).

---

## Местонахождение кода

```
src/graph/
├── __init__.py    # экспорт ServicePassport, PassportRegistry
└── passport.py    # реализация (stdlib-only: dataclasses, collections, re, math)
```

Публичный API:

```python
# Построение
passport = ServicePassport.from_findings("ts-travel-service", findings)
registry = PassportRegistry()
registry.add(passport)
registry.build_call_graph()       # обязательно после всех add()

# Запросы
registry.affected_by("svc", depth=2)          # Level-1 BFS → {name: hop}
registry.similar_services("svc", top_k=5)     # n-грамм схожесть паспортов
registry.stats()                              # сводная статистика
passport.to_text()                            # текст для векторизации
passport.similarity_to(other)                 # cosine по паспортам
```

---

## Связанные ADR

- ADR-006 Engine Architecture — место PassportRegistry в pipeline
- ADR-031 SimilarityIndex — аналогичный подход для файлов
- ADR-032 n-gram hint lookup — алгоритм cosine, переиспользован в `similarity_to()`
- ADR-001 Parser Strategy — Java AST adapter, нужен для endpoints и полных call edges
