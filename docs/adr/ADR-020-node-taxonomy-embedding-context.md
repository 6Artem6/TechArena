# ADR-020: Таксономия узлов, шаблоны паттернов, контекстные эмбеддинги и извлечение информации

**Статус:** Принято  
**Дата:** 2026-10-13  
**Связанные:** ADR-002, ADR-004, ADR-014, ADR-016, ADR-017, ADR-018, ADR-019

---

## Контекст

Три связанные проблемы:

1. **Не все узлы одинаковы**: SERVICE-узел изменяется редко — он якорный. METHOD-узел — реальная точка изменений. BFS и эмбеддинги должны это учитывать.
2. **Паттерны — не только строки**: `getServiceUrl({X})` содержит переменный слот. Векторные паттерны — вообще не строка. Единая `rules` table нужно расширить.
3. **Контекст вокруг элемента** радикально меняет смысл эмбеддинга: `createOrder` — слабый вектор; `PreserveServiceImpl::preserve → createOrder в шаге 4 после checkSecurity` — сильный.

---

## Решение A: Таксономия узлов — Anchor vs Active

### Классификация

```
Anchor (якорные)       Active (активные)
───────────────────    ──────────────────
SERVICE                METHOD
PACKAGE_ROLE           CLASS
INTERFACE              FIELD
DTO (shared)           CONFIG_SECTION
                       UnknownDynamicTarget
```

**Anchor-узлы** — структурные опоры графа. Редко являются прямыми точками изменений, но через них проходит большинство рёбер.

**Active-узлы** — реальные единицы изменений. BFS стартует отсюда.

### Разное поведение BFS

```python
NODE_BFS_ROLE: dict[str, str] = {
    "SERVICE":              "anchor",  # изменился сам сервис целиком (редко)
    "PACKAGE_ROLE":         "anchor",  # никогда не стартовая точка
    "INTERFACE":            "anchor",  # изменение интерфейса → все impl
    "DTO":                  "anchor",  # изменение DTO → все потребители
    "CLASS":                "active",
    "METHOD":               "active",  # приоритетная стартовая точка
    "FIELD":                "active",
    "CONFIG_SECTION":       "active",
    "UnknownDynamicTarget": "active",
}

def bfs_blast_radius(start_nodes: list[NodeId], graph, max_depth: int = 6) -> set[NodeId]:
    """BFS с разным поведением для anchor vs active."""
    visited = set()
    # Для anchor-старта предупреждаем: blast radius будет большим
    for n in start_nodes:
        if graph.nodes[n].get("bfs_role") == "anchor":
            log.warning("anchor_node_start", node=n,
                        note="blast radius может быть очень большим")
    # ... стандартный BFS с TRAVERSAL_POLICY (ADR-005)
```

### Критичность по типу узла

| Тип | Base criticality | Причина |
|---|---|---|
| `SERVICE` | 1.0 | Весь сервис — максимальная область |
| `INTERFACE` | 0.9 | Изменение контракта затрагивает все реализации |
| `DTO` (ts-common) | 0.85 | Shared contract, много потребителей |
| `CLASS` | 0.7 | Локализован внутри сервиса |
| `METHOD` | 0.6 | Минимальная единица изменения |
| `CONFIG_SECTION` | 0.8 | Конфиг влияет на runtime поведение |
| `UnknownDynamicTarget` | 1.5 | Неизвестен таргет → максимальный риск |

---

## Решение B: Схема хранения паттернов с шаблонными слотами

### Расширение таблицы `rules` (ADR-018)

Добавляем поддержку **шаблонных паттернов со слотами** и **векторных паттернов**:

```sql
ALTER TABLE rules ADD COLUMN IF NOT EXISTS
    template_slots  JSONB;
    -- Пример: {"SERVICE_NAME": "string", "METHOD_NAME": "string"}
    -- Слоты для заполнения при применении паттерна

ALTER TABLE rules ADD COLUMN IF NOT EXISTS
    vector_only     BOOLEAN DEFAULT FALSE;
    -- TRUE: паттерн — только вектор, нет текстового выражения (centroid кластера)
```

### Пять типов паттернов

| pattern_type | Описание | Пример |
|---|---|---|
| `string_match` | Точная строка | `@RestController` |
| `regex` | Регулярное выражение | `restTemplate\.(get\|post\|exchange)` |
| `template` | Строка со слотами `{X}` | `getServiceUrl({SERVICE_NAME})` |
| `tree_sitter_query` | AST-запрос | `(method_invocation name: (identifier) @n (#match? @n "exchange"))` |
| `vector_centroid` | Нет текста, только embedding | centroid кластера unknowns |

### Применение в поиске: каскад

```python
class RuleEngine:

    def match(self, scope: str, text: str, ast_node=None,
              context: str = "") -> RuleMatch | None:
        """Каскадный поиск: точный → шаблон → векторный."""
        candidates = self._rules_by_scope.get(scope, [])

        # 1. Exact + regex: O(n), быстро
        for rule in candidates:
            if rule.pattern_type == "string_match" and rule.pattern == text:
                return RuleMatch(rule, "exact", conf=1.0)
            if rule.pattern_type == "regex":
                if re.search(rule.pattern, text):
                    return RuleMatch(rule, "regex", conf=rule.confidence_float)

        # 2. Template со слотами: извлечь переменные
        for rule in candidates:
            if rule.pattern_type == "template":
                bindings = template_match(rule.pattern, text, rule.template_slots)
                if bindings is not None:
                    return RuleMatch(rule, "template", conf=0.90,
                                     bindings=bindings)

        # 3. tree-sitter query: только если есть AST
        if ast_node is not None:
            for rule in candidates:
                if rule.pattern_type == "tree_sitter_query":
                    if ts_query_matches(rule.pattern, ast_node):
                        return RuleMatch(rule, "ts_query", conf=0.92)

        # 4. Vector: BGE-M3 embedding + cosine threshold
        input_text = build_embedding_input(text, context)
        query_vec = bge_m3.embed(input_text)
        best = nearest_rule_by_vector(query_vec, candidates,
                                      threshold=VECTOR_THRESHOLDS[scope])
        if best:
            return RuleMatch(best.rule, "vector", conf=best.similarity,
                             confidence_override="MEDIUM")
        return None


def template_match(template: str, text: str,
                   slots: dict[str, str]) -> dict[str, str] | None:
    """Паттерн 'getServiceUrl({SERVICE_NAME})' → {'SERVICE_NAME': 'ts-order-service'}."""
    # Превращаем шаблон в regex: {X} → именованная группа
    regex = template
    for slot_name in slots:
        regex = regex.replace(f"{{{slot_name}}}", f"(?P<{slot_name}>[^,)]+)")
    regex = re.escape(regex).replace(r"\(\?P", "(?P")  # разэкранируем группы
    m = re.search(regex, text)
    return m.groupdict() if m else None
```

---

## Решение C: Bi-encoder для кластеризации и пороги сходства

### BGE-M3 как bi-encoder

BGE-M3 — это **bi-encoder** (независимое кодирование запроса и документа). Именно поэтому:
- Batch encoding unknowns: O(n) — один проход
- Pairwise similarity после encoding: O(n²) или O(n log n) с FAISS
- НЕ cross-encoder (O(n²) вызовов модели) — слишком дорого для кластеризации

```python
# Правильная схема кластеризации:
unknowns: list[str] = [u.text for u in unknown_patterns]
embeddings = bge_m3.embed_batch(unknowns)           # O(n) — один батч-проход
labels = dbscan(embeddings, eps=1 - CLUSTER_THRESHOLD,  # cosine distance
                min_samples=CLUSTER_MIN_SIZE)
# Для каждого кластера — один вызов Ollama (O(кластеры), не O(unknowns))
```

### Пороги по ролям

Разные роли имеют разные семантические пространства — единый порог неверен:

```python
VECTOR_THRESHOLDS: dict[str, float] = {
    # role: min_cosine_similarity для "это совпадает с паттерном"
    "ENV_PATTERN":     0.82,   # env var semantics узкие, высокий порог
    "SHADOW_CODE":     0.75,   # код более разнообразен
    "CODE_STRUCTURE":  0.88,   # AST-сигнатуры точные
    "UNKNOWN_PATTERN": 0.80,   # для назначения правила
    "SEMANTIC_SEARCH": 0.65,   # семантический поиск — более широкий
    "PACKAGE_ROLE":    0.78,   # имена директорий, средняя вариабельность
}

# Для DBSCAN: eps = 1 - порог (cosine distance = 1 - cosine similarity)
CLUSTER_THRESHOLDS: dict[str, float] = {
    "UNKNOWN_PATTERN": 1 - 0.80,   # eps = 0.20
    "ENV_PATTERN":     1 - 0.82,   # eps = 0.18
    "SHADOW_CODE":     1 - 0.75,   # eps = 0.25
}

# Зоны уверенности (общие)
# sim >= 0.88 → HIGH confidence (принять автоматически)
# sim 0.70–0.88 → MEDIUM confidence (принять с пометкой)
# sim 0.55–0.70 → LOW confidence (в очередь неопределённостей)
# sim < 0.55 → NO_MATCH (USES_MAGIC_CALL или пропуск)
CONFIDENCE_ZONES = [(0.88, "HIGH"), (0.70, "MEDIUM"), (0.55, "LOW")]
```

### Cross-encoder для финальной валидации (не кластеризации)

Cross-encoder (например, `BGE-reranker`) используем **только** как второй проход после bi-encoder:
- bi-encoder даёт top-K кандидатов быстро
- cross-encoder из них выбирает лучший точно
- Применяется при конфликте: несколько правил с sim > 0.70

---

## Решение D: Контекстные эмбеддинги — AST + окружение + комментарии

### Проблема голого имени

```
embed("createOrder")            → слабый вектор, много коллизий
embed(full_context_below)       → сильный вектор, уникальный для узла
```

### Шаблон контекстного ввода для эмбеддинга

```python
def build_node_embedding_input(node: ParsedNode, ctx: NodeContext) -> str:
    """Сборка контекстного ввода для GraphCodeBERT / BGE-M3.

    Каждый компонент добавляет семантическую ось:
    - [SERVICE]/[CLASS]: иерархическое местонахождение
    - [ANNOTATIONS]: роль в Spring (controller, service, ...)
    - [COMMENT_STEPS]: бизнес-назначение метода
    - [LOGGER]: структурированные метки из логгера
    - [CALLS]: исходящие зависимости
    - [CALLED_BY]: входящие зависимости
    """
    parts = [
        f"[SERVICE:{ctx.service}]",
        f"[PKG:{ctx.package}]",
        f"[CLASS:{ctx.class_name}]",
    ]
    if node.annotations:
        parts.append(f"[ANN:{' '.join(node.annotations)}]")
    if ctx.step_comments:
        # "//4.send the order request" → "[STEP:4:send the order request]"
        steps = " ".join(f"{s.num}:{s.text}" for s in ctx.step_comments)
        parts.append(f"[STEPS:{steps}]")
    if ctx.logger_tags:
        # "[preserve][Step 4][Do Order]" → "[LOG:preserve Step4 DoOrder]"
        parts.append(f"[LOG:{' '.join(ctx.logger_tags)}]")
    if ctx.calls:
        parts.append(f"[CALLS:{' '.join(ctx.calls[:5])}]")  # top-5
    if ctx.called_by:
        parts.append(f"[CALLED_BY:{' '.join(ctx.called_by[:3])}]")
    if node.return_type:
        parts.append(f"[RET:{node.return_type}]")
    # Первые 120 символов сигнатуры метода (не тело)
    parts.append(f"[SIG:{ctx.signature[:120]}]")
    return " ".join(parts)
```

**Пример для `PreserveServiceImpl#preserve`:**
```
[SERVICE:ts-preserve-service] [PKG:preserve.service] [CLASS:PreserveServiceImpl]
[ANN:@Service] [STEPS:1:detect ticket scalper 2:Querying contact information
3:Check tickets num 4:send order request 5:Check insurance 6:food order
7:add consign 8:send notification]
[LOG:preserve Step1 CheckSecurity Step4 DoOrder Step8 Notification]
[CALLS:ts-security-service ts-contact-service ts-travel-service ts-order-service
ts-basic-service ts-assurance-service ts-food-service ts-consign-service ts-notification-service]
[RET:Response] [SIG:public Response preserve(OrderTicketsInfo oti, HttpHeaders headers)]
```

Этот вектор уникально идентифицирует метод в пространстве семантики.

---

## Решение E: Комментарии как информационный слой — полная таксономия

Результаты исследования train-ticket репозитория выявили 7 классов комментариев с разной ценностью для анализа.

### E.1 Пронумерованные шаги бизнес-флоу

**Паттерн:** `//N.описание_шага` в сервисных методах

**Примеры из кода:**
```java
// PreserveServiceImpl.java, ts-preserve-service:
//1.detect ticket scalper
//2.Querying contact information -- modification, mediated by the underlying information micro service
//3.Check the info of train and the number of remaining tickets
//4.send the order request and set the order information
//5.Check insurance options
//6.Increase the food order
//7.add consign
//8.send notification
```

**Что извлекаем:**
```python
@dataclass
class FlowStep:
    num: int
    description: str
    next_call: str | None   # первый RestTemplate-вызов после этого комментария

FLOW_STEP_PATTERN = re.compile(r"//\s*(\d+)\.\s*(.+)")

def extract_flow_steps(method_text: str) -> list[FlowStep]:
    steps = []
    for m in FLOW_STEP_PATTERN.finditer(method_text):
        steps.append(FlowStep(num=int(m.group(1)), description=m.group(2).strip()))
    return steps
```

**Использование:** `flow_steps` как метаданные METHOD-узла → контекст эмбеддинга → semantic search ("найди метод, который уведомляет о бронировании") → node #8.

---

### E.2 Структурированные logger-строки

**Паттерн:** `LOGGER.[info|warn|error]("[service][Step N][Action][State]", ...)`

**Примеры:**
```java
LOGGER.info("[preserve][Step 1][Check Security Complete][AccountId: {}]")
LOGGER.error("[preserve][Step 4][Do Order][Create Order Fail][OrderId: {}]")
LOGGER.warn("[preserve][Step 3][Check seat][Seat Not Enough]")
```

**Регекс-извлечение:**
```python
LOGGER_TAG_PATTERN = re.compile(r'\[([^\[\]]+)\]')

def extract_logger_tags(line: str) -> list[str]:
    """Из строки логгера извлекает теги как метаданные."""
    if not re.search(r'LOGGER\.(info|warn|error|debug)', line):
        return []
    return LOGGER_TAG_PATTERN.findall(line)
    # "[preserve][Step 1][Check Security Complete]"
    # → ["preserve", "Step 1", "Check Security Complete"]
```

**Использование:** теги первого уровня = `service_tag`, второго = `step_tag`, третьего = `action_tag`. Все идут в контекст эмбеддинга.

---

### E.3 Закомментированные @Value = Shadow Config Dependency

**Паттерн:** `//    @Value("${service-url}")` + `//    String service_url;`

**Примеры из кода:**
```java
// ts-order-service/OrderServiceImpl.java:
//    @Value("${station-service.url}")
//    String station_service_url;

// ts-order-other-service/OrderOtherServiceImpl.java:
//    @Value("${station-service.url}")
//    String station_service_url;

// ts-admin-user-service/AdminUserServiceImpl.java:
//    @Value("${user-service.url}")
//    String user_service_url;
//    private final String USER_SERVICE_IP_URI = user_service_url + "/api/v1/userservice/users";
```

**Это не просто shadow code** — это история архитектурной трансформации: сервисы переехали с `@Value`-конфигурации на `getServiceUrl()` паттерн, но закомментированный код остался.

```python
COMMENTED_VALUE_PATTERN = re.compile(
    r'//\s*@Value\s*\(\s*["\']?\$\{([^}]+)\}["\']?\s*\)'
)
COMMENTED_FIELD_PATTERN = re.compile(
    r'//\s*(?:private|protected)?\s*\w+\s+(\w+_url|\w+_service_url)\s*;'
)
```

**Тип ребра:** `SHADOW_CALLS_SERVICE` с confidence=0.90 (выше обычного shadow, т.к. это явная конфиг-зависимость).

---

### E.4 URL-документация в inline-комментариях

**Паттерн:** `// ts-service-name:port/api/path` рядом с активным кодом (не закомментированным)

**Примеры из ts-rebook-service:**
```java
// ts-travel-service:12346/travel/getTripAllDetailInfo
requestUrl = travel_service_url + "/api/v1/travelservice/trip_detail";

//ts-order-service:12031/order/create
requestUrl = order_service_url + "/api/v1/orderservice/order";

//ts-order-other-service:12032/orderOther/create
requestUrl = order_other_service_url + "/api/v1/orderOtherService/orderOther";
```

**Это НЕ shadow** — код активный. Это **URL-документация**: старый путь (port-based) рядом с новым (service-name-based). Ценнейшая информация для RouteIndex!

```python
URL_DOC_PATTERN = re.compile(
    r'//\s*(ts-[\w-]+):(\d+)(/[\w/]+)',
    re.IGNORECASE
)

def extract_url_hints(line: str) -> UrlHint | None:
    """Извлекает legacy URL из документационного комментария."""
    m = URL_DOC_PATTERN.search(line)
    if m:
        return UrlHint(
            service=m.group(1),
            port=int(m.group(2)),
            path=m.group(3),
            is_legacy=True  # старый путь, используется для обогащения RouteIndex
        )
    return None
```

**Использование:** обогащает RouteIndex legacy-путями. Особенно важно для ts-rebook-service, который имеет branch-логику на основе tripId.

---

### E.5 TODO с намерением рефакторинга

**Паттерн:** `// TODO: change to X`

**Примеры:**
```java
// ts-preserve-service (строка 260):
// TODO: change to async message serivce
// sendEmail(notifyInfo, headers);

// ts-cancel-service (строка 86):
// TODO: change to async message serivce
```

**Это планируемое архитектурное изменение**. Сейчас: синхронный вызов email. Планируется: async message.

```python
@dataclass
class TodoIntent:
    text: str
    intent_type: str     # "async_refactor" | "service_replace" | "remove" | "fix" | "other"
    target_service: str | None

TODO_ASYNC_PATTERN = re.compile(r"TODO.*async.*message|TODO.*queue|TODO.*rabbit", re.I)

def classify_todo(todo_text: str) -> TodoIntent:
    if TODO_ASYNC_PATTERN.search(todo_text):
        return TodoIntent(text=todo_text, intent_type="async_refactor",
                          target_service=None)
    # ... другие паттерны
```

**Использование в графе:** если `intent_type="async_refactor"` → добавить metadata `planned_async: true` к ближайшему CALLS_SERVICE ребру. При blast radius: предупреждение в JSON что ребро под рефакторингом.

---

### E.6 Предупреждения о неполноте зависимостей

**Паттерн:** `// This relies on a lot of other services, not completely modified`

**Пример:** `ts-food-service/FoodController.java:88`

**Это маркер неопределённости** — разработчик знал что связей больше, но не все реализовал.

```python
INCOMPLETE_DEP_PATTERNS = [
    re.compile(r"relies on.*services?", re.I),
    re.compile(r"not completely (modified|implemented|finished)", re.I),
    re.compile(r"TODO:.*service.*depend", re.I),
]
```

**Использование:** `has_incomplete_dependencies: true` → BFS для этого узла добавляет `UnknownDynamicTarget` с весом 1.2. Blast radius помечается как `potentially_underestimated`.

---

### E.7 Технические маркеры (пропустить)

| Паттерн | Пример | Действие |
|---|---|---|
| `//NOSONAR` | `order.setTravelDate("2022"); //NOSONAR` | Пропустить — suppression для SonarQube |
| `// close csrf`, `// close session` | SecurityConfig | Пропустить — конфиг-инструкции без семантики для графа |
| `//Default Constructor` | entity классы | Пропустить — документация боилерплейта |
| Китайский текст | `// 测试` | Пропустить — per ADR-016 |

```python
SKIP_PATTERNS = [
    r"//\s*NOSONAR",
    r"//\s*Default Constructor",
    r"//\s*close\s+(csrf|session|cache)",
    r"[一-鿿]",   # китайские символы
]
```

---

### Сводная таблица классов комментариев

| Класс | Pattern regex | Выходное поле | Ценность |
|---|---|---|---|
| Flow steps | `//\s*\d+\.` | `flow_steps[]` | Высокая — бизнес-контекст |
| Logger tags | `LOGGER\.\w+\("\[` | `logger_tags[]` | Высокая — структурированные метки |
| Shadow @Value | `//\s*@Value` | `SHADOW_CALLS_SERVICE` (conf=0.90) | Высокая — архитектурная история |
| URL hints | `//\s*ts-\w+:\d+/` | `route_hints[]` → RouteIndex | Высокая — legacy маршруты |
| TODO async | `TODO.*async|TODO.*queue` | `planned_async: true` | Средняя — планируемое изменение |
| Incomplete deps | `relies on.*services` | `has_incomplete_deps: true` | Средняя — неопределённость |
| NOSONAR, закрытый csrf | `NOSONAR`, `close (csrf\|session)` | Пропустить | Нет |

---

## Решение F: Многоуровневое извлечение информации

Каждый файл/узел анализируется на нескольких уровнях — от синтаксического к намеренческому:

```
Уровень 1 — Синтаксический (tree-sitter):
    Аннотации, типы, сигнатуры, импорты, пакеты

Уровень 2 — Семантический (regex + SymbolIndex):
    Имена методов → role hints, суперклассы, реализованные интерфейсы

Уровень 3 — Поведенческий (комментарии):
    Flow steps, logger tags, URL hints → execution flow

Уровень 4 — Намеренческий (TODO/FIXME/Shadow):
    Планируемые изменения, deprecated code, неполные зависимости

Уровень 5 — Векторный (BGE-M3 / GraphCodeBERT):
    Семантическое сходство для fuzzy matching и кластеризации
```

```python
@dataclass
class RichNodeMetadata:
    # Уровень 1: синтаксис
    annotations: list[str]
    return_type: str | None
    parameters: list[str]
    imports: list[str]

    # Уровень 2: семантика
    arch_role: str | None           # http_handler | business_logic | ...
    parent_class: str | None
    implemented_interfaces: list[str]

    # Уровень 3: поведение
    flow_steps: list[FlowStep]       # //1. //2. //3. ...
    logger_tags: list[str]           # [service][step][action]
    route_hints: list[UrlHint]       # // ts-service:port/path

    # Уровень 4: намерение
    deprecated: bool
    has_bug_note: bool
    planned_async: bool              # TODO: change to async
    has_incomplete_deps: bool        # relies on a lot of other services

    # Уровень 5: вектор (заполняется отдельно)
    embedding: list[float] | None    # GraphCodeBERT / BGE-M3
    embedding_role: str | None       # CODE_STRUCTURE | SEMANTIC_SEARCH
```

---

## .github/workflows: что даёт

**Java version: 8** → подтверждает стек (tree-sitter-java grammar должен покрывать Java 8).  
**Maven build**: `mvn -Dmaven.test.skip=true clean package` → при index-сборке можно **пропустить** `target/` директории (скомпилированные .class файлы в репо).  
**CI на тегах** `v[0-9]+.[0-9]+.[0-9]+` → история тегов = история версий (если нужно для контекста).

```python
# Добавить в IsolatedParser (ADR-019):
EXCLUDED_DIRS = {
    "target",           # Maven build output
    "node_modules",     # npm
    ".gradle",          # Gradle cache
    "__pycache__",
    ".git",
}

def iter_source_files(service_dir: Path) -> Iterator[Path]:
    for f in service_dir.rglob("*"):
        if any(excl in f.parts for excl in EXCLUDED_DIRS):
            continue
        if f.suffix in {".java", ".py", ".go", ".js", ".ts"}:
            yield f
```

---

## Связи с другими ADR (что меняется)

| ADR | Изменение |
|---|---|
| ADR-002 | `RichNodeMetadata` заменяет текущий набор атрибутов; добавляются `flow_steps`, `logger_tags`, `route_hints`, `planned_async`, `has_incomplete_deps` |
| ADR-003 | URL hints из комментариев дополняют RouteIndex (E.4); Shadow `@Value` → SHADOW_CALLS_SERVICE conf=0.90 (E.3) |
| ADR-005 | Anchor vs Active taxonomy влияет на BFS стартовую точку и предупреждения; base criticality по типу узла |
| ADR-014 | Контекстный шаблон `build_node_embedding_input()`; per-role similarity thresholds; bi-encoder vs cross-encoder разграничение |
| ADR-016 | Shadow @Value (E.3) = новый под-тип shadow pipeline с conf=0.90; URL hints (E.4) = НЕ shadow, отдельный класс |
| ADR-018 | `template` pattern_type + `template_slots` колонка; `vector_centroid` pattern_type |
| ADR-019 | `EXCLUDED_DIRS` в `iter_source_files()`; TODO async intent → `planned_async` флаг |

## Последствия

- `RichNodeMetadata` увеличивает размер узловых атрибутов ~в 2 раза, но значительно улучшает контекст эмбеддингов и точность поиска.
- Flow steps из комментариев (`//1.detect ticket scalper`) дают прямую семантику метода без ML — быстро и бесплатно.
- Logger tags извлекаются regex за < 1ms на файл — нет overhead.
- Per-role similarity thresholds устраняют одну из главных причин ложных срабатываний.
- `target/` exclusion ускоряет индексацию (Maven может генерировать тысячи .class файлов).
- Shadow @Value conf=0.90 vs обычные shadow 0.4 — архитектурно обоснованно: явный тег конфига надёжнее общего shadow кода.
