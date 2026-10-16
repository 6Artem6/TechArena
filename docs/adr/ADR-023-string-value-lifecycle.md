# ADR-023: Жизненный цикл строк и конфигов в коде — классификация, резолвинг, SymbolIndex с FQN

**Статус:** Принято  
**Дата:** 2026-10-13  
**Связанные:** ADR-003, ADR-021, ADR-022  
**Поглощает:** ADR-024 (SymbolIndex FQN + bi-encoder реранкер + аномалии train-ticket → секции H и I)

---

## Контекст

Три проблемы, которые нельзя решить простым grep:

1. **Строка в коде — это конфиг или бизнес-данные?** `"email"` — очередь RabbitMQ или почтовый адрес? `"PAID"` — статус заказа или название сервиса?
2. **Переменная-конфиг может жить долго**: `url = getServiceUrl("x")` → `uri = url + "/path"` → `exchange(uri + "/" + id)`. Нужно отследить всю цепочку.
3. **Ссылка на константу**: `@RabbitListener(queues = Queues.queueName)` — нужно разрезолвить `Queues.queueName` в `"email"` через SymbolIndex, а не брать буквальный текст.

**Реальные паттерны из train-ticket:**

```java
// Паттерн A: цепочка getServiceUrl → промежуточная → финальный URL
String user_service_url = getServiceUrl("ts-user-service");        // def1
String USER_SERVICE_IP_URI = user_service_url + "/api/v1/userservice/users";  // def2
restTemplate.exchange(USER_SERVICE_IP_URI + "/" + userId, ...)     // USE — финальный URL

// Паттерн B: инициализация пустой + условное присваивание
String requestUrl = "";                                             // def0 (пустой)
if (tripId.startsWith("G")) requestUrl = travel_service_url + "/api/v1/travelservice/trip_detail";  // def_branch1
else requestUrl = travel2_service_url + "/api/v1/travel2service/trip_detail";                       // def_branch2
restTemplate.exchange(requestUrl, ...)                              // USE — два возможных значения

// Паттерн C: константа из другого класса
public class Queues { public final static String queueName = "email"; }
@RabbitListener(queues = Queues.queueName)     // нужно разрезолвить → "email"
convertAndSend(Queues.queueName, val)           // аналогично

// Паттерн D: @Value с default
@Value("${email_address:trainticket_notify@163.com}") private String emailAddr;
// key = "email_address", default = "trainticket_notify@163.com"

// Паттерн E: строка прямо в аргументе (без переменной)
restTemplate.getForObject("http://rest-service-external:16100/greet", Boolean.class)
// URL жёстко вкодирован — dead service ref (ADR-021)

// Паттерн F: бизнес-строка, НЕ конфиг
if (order.getStatus() == "PAID") { ... }        // → НЕ создавать ребро
mailService.sendEmail(mail, "preserve_success.ftl")  // имя шаблона, НЕ конфиг
```

---

## A. Жизненный цикл переменной-конфига

Переменная проходит через фазы. Нужно отследить их все для точного резолвинга.

```
ИСТОЧНИК (откуда берётся значение):
  S1  Строковый литерал в поле класса
        private static final String URL = "http://ts-x:port";
  S2  @Value инъекция с опциональным default
        @Value("${key:default}") private String url;
  S3  Вызов helper-функции (getServiceUrl, buildUrl, ...)
        String url = getServiceUrl("ts-x");
  S4  Строка прямо в аргументе вызова (inline)
        exchange("http://ts-x/path", ...)
  S5  Ссылка на статическую константу другого класса
        Queues.queueName → "email"
  S6  System.getenv / os.environ / process.env
        String url = System.getenv("SERVICE_URL");

ПЕРЕДАЧА (как значение переходит к использованию):
  T1  Присваивание новой локальной переменной
        String uri = url + "/api/v1/path";
  T2  Условное присваивание (if/else/switch)
        if (cond) uri = urlA + path; else uri = urlB + path;
  T3  Конкатенация суффикса
        exchange(uri + "/" + id, ...)   ← суффикс динамический
  T4  Передача в метод как параметр
        doRequest(uri, method)          ← uri внутри метода
  T5  Поле класса → метод
        this.serviceUrl + "/path"       ← поле, установленное @Value или конструктором

ЗАТИРАНИЕ (когда старое значение больше не актуально):
  O1  Переприсваивание переменной
        url = System.getenv("OVERRIDE"); if (url != null) { ... }
  O2  Null/empty проверка → замена дефолтом
        if (url == null || url.isEmpty()) url = DEFAULT_URL;
  O3  Метод возвращает новое значение
        url = processUrl(url);          ← зависит от тела processUrl
```

### Полный граф резолвинга (Value Flow Graph)

```python
@dataclass
class ValueNode:
    var_name: str
    value: str | None           # известное строковое значение
    sources: list[str]          # имена переменных-источников
    phase: str                  # S1-S6, T1-T5, O1-O3
    stmt_index: int
    is_conditional: bool        # True если значение зависит от branch
    possible_values: list[str]  # для T2: список возможных значений

def build_value_flow(method_node, source_text: str, symbol_index: SymbolIndex) -> dict[str, ValueNode]:
    """
    Строит граф потока значений для всех переменных метода.
    Без выполнения кода — только статический обход AST.
    """
    flow: dict[str, ValueNode] = {}
    
    for stmt_idx, stmt in enumerate(iter_stmts(method_node)):
        var, rhs = extract_assignment(stmt, source_text)
        if var is None:
            continue
        
        node = classify_rhs(rhs, flow, symbol_index, stmt_idx)
        flow[var] = node
    
    return flow

def classify_rhs(rhs: str, flow: dict, index: SymbolIndex, stmt_idx: int) -> ValueNode:
    """Определяет фазу и значение правой части присваивания."""
    
    # S3: вызов helper-функции
    m = re.match(r'getServiceUrl\s*\(\s*"([^"]+)"', rhs)
    if m:
        return ValueNode(value=f"http://{m.group(1)}", phase="S3", ...)
    
    # S5: ссылка на статическую константу  
    m = re.match(r'(\w+)\.(\w+)', rhs)
    if m:
        const_val = index.get_static_field(m.group(1), m.group(2))
        if const_val:
            return ValueNode(value=const_val, phase="S5", ...)
    
    # S4: строковый литерал
    m = re.match(r'"([^"]+)"', rhs.strip())
    if m:
        return ValueNode(value=m.group(1), phase="S4", ...)
    
    # T1: конкатенация переменной + строки
    m = re.match(r'(\w+)\s*\+\s*"([^"]+)"', rhs)
    if m and m.group(1) in flow:
        base = flow[m.group(1)].value or ""
        return ValueNode(value=base + m.group(2), sources=[m.group(1)], phase="T1", ...)
    
    # T2: условное — помечаем как is_conditional, отслеживаем separately через CFG
    ...
    
    return ValueNode(value=None, phase="UNKNOWN", ...)
```

---

## B. Классификация строк: конфиг vs бизнес-логика

Три уровня: структурная проверка (быстро) → regex (средне) → векторный поиск (если всё остальное неоднозначно).

### B.1 Структурная проверка (быстро, без ML)

```python
def structural_string_class(s: str) -> str | None:
    """
    Быстрая классификация по структуре строки.
    Возвращает класс если уверен, None если нужна следующая ступень.
    """
    # Явный HTTP URL → SERVICE_URL
    if re.match(r'https?://[\w\-\.]+(?::\d+)?', s):
        return "SERVICE_URL"
    
    # host:port без http → SERVICE_URL (если host похож на имя сервиса)
    if re.match(r'[a-z][a-z0-9\-]+-service:\d{4,5}', s):
        return "SERVICE_URL"
    
    # /api/v1/ путь → API_PATH (не сервис, а путь к нему)
    if re.match(r'/api/v[0-9]+/', s):
        return "API_PATH"
    
    # Явные статусы заказов → BUSINESS_STATUS
    if s.upper() in {"PAID", "NOTPAID", "CANCEL", "USED", "CHANGE", "COLLECTED",
                     "SUCCESS", "FAILED", "ERROR", "PENDING", "PROCESSING"}:
        return "BUSINESS_STATUS"
    
    # .ftl/.html/.xml шаблоны → TEMPLATE_NAME
    if re.match(r'[\w_]+\.(ftl|html|xml|json)$', s):
        return "TEMPLATE_NAME"
    
    # UUID / hash → DATA
    if re.match(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}', s):
        return "DATA"
    
    # SQL / сообщения логгера → DATA
    if any(kw in s.lower() for kw in ['select ', 'insert ', 'update ', 'delete from']):
        return "SQL_QUERY"
    
    return None  # неоднозначно → следующий уровень
```

### B.2 Regex-эвристики для конкретных типов

```python
STRING_CLASS_PATTERNS = {
    "QUEUE_NAME": [
        # Короткое слово строчными / подчёркиванием — потенциальное имя очереди
        # НО только в контексте RabbitMQ/Kafka вызова (Two-Phase: сначала найти контекст)
        re.compile(r'^[a-z][a-z0-9_]{2,30}$'),
    ],
    "SERVICE_NAME": [
        # ts-xxx-service паттерн
        re.compile(r'^ts-[a-z][a-z0-9\-]+(?:-service)?$'),
    ],
    "ENV_KEY": [
        # UPPER_SNAKE_CASE — ключ env var
        re.compile(r'^[A-Z][A-Z0-9_]{2,40}$'),
    ],
    "CONFIG_PROPERTY_KEY": [
        # spring.cloud.xxx, server.port, etc.
        re.compile(r'^[\w\-]+(?:\.[\w\-]+){1,4}$'),
    ],
    "API_PATH_SUFFIX": [
        re.compile(r'^/[a-z][a-z0-9/\-_{}]+$'),  # /order/{id}/cancel
    ],
}
```

**Проблема QUEUE_NAME**: `"email"` может быть очередью или строкой письма. Regex не различит.

### B.3 Контекстная проверка (перед векторным поиском)

Контекст = тип вызова, в котором используется строка.

```python
STRING_CONTEXT_RULES = {
    # Если строка — первый аргумент convertAndSend → QUEUE_NAME
    ("convertAndSend", 0): "QUEUE_NAME",
    # Если строка — первый аргумент send (KafkaTemplate) → TOPIC_NAME  
    ("kafkaTemplate.send", 0): "TOPIC_NAME",
    # Если строка — аргумент queues= в @RabbitListener → QUEUE_NAME
    ("@RabbitListener", "queues"): "QUEUE_NAME",
    # Если строка — первый аргумент exchange → SERVICE_URL или API_PATH
    ("exchange", 0): "SERVICE_URL_OR_PATH",
    # Если строка — второй аргумент sendEmail → TEMPLATE_NAME
    ("sendEmail", 1): "TEMPLATE_NAME",
}

def classify_by_call_context(string_val: str, call_name: str, arg_index: int) -> str | None:
    return STRING_CONTEXT_RULES.get((call_name, arg_index))
```

**Это решает проблему `"email"` в `convertAndSend("email", val)` → QUEUE_NAME, HIGH conf.**

### B.4 Векторный поиск (последняя инстанция)

Применяется только если структура + regex + контекст не дали однозначного ответа.

```python
STRING_CLASS_EXAMPLES = {
    "SERVICE_URL": [
        "http://ts-order-service:12031",
        "http://ts-station-service:12345",
        "http://rest-service-external:16100/greet",
        "lb://ts-gateway-service",
    ],
    "QUEUE_NAME": [
        "email",
        "food_delivery",
        "order-created",
        "notification",
    ],
    "API_PATH": [
        "/api/v1/orderservice/order",
        "/api/v1/stationservice/stations/namelist",
        "/api/v1/notifyservice/notification/order_cancel_success",
    ],
    "BUSINESS_STATUS": [
        "PAID", "NOTPAID", "CANCEL", "ORDER_CREATED",
        "preserve_success", "order_cancel_success",
    ],
    "CONFIG_KEY": [
        "station-service.url", "email_address", "rabbitmq_host",
        "NOTIFICATION_MYSQL_HOST", "NACOS_ADDRS",
    ],
    "ENV_DEFAULT": [
        "trainticket_notify@163.com", "localhost", "root",
        "127.0.0.1:8848",
    ],
    "TEMPLATE_NAME": [
        "preserve_success.ftl", "order_create_success.ftl",
        "order_changed_success.ftl", "order_cancel_success.ftl",
    ],
}

def classify_string_by_vector(
    string_val: str,
    embedding_model,
    cache: ConceptEmbeddingCache,
    threshold: float = 0.75,
) -> tuple[str, float]:
    """
    Классифицирует строку через косинусное сходство с примерами классов.
    Возвращает (class_name, confidence).
    """
    vec = embedding_model.encode([string_val], normalize=True)[0]
    
    best_class, best_sim = "UNKNOWN", 0.0
    for cls, examples in STRING_CLASS_EXAMPLES.items():
        example_vecs = cache.get_example_vecs(f"string_class_{cls}")
        if example_vecs is None:
            continue
        sim = float(np.max(example_vecs @ vec))   # max cosine over all examples
        if sim > best_sim:
            best_sim, best_class = sim, cls
    
    if best_sim < threshold:
        return "UNKNOWN", best_sim
    return best_class, best_sim
```

### Полный pipeline классификации строки

```python
def classify_string(
    string_val: str,
    call_context: tuple[str, int] | None,
    embedding_model,
    cache: ConceptEmbeddingCache,
) -> StringClassification:
    
    # Ступень 1: структура (O(1))
    cls = structural_string_class(string_val)
    if cls:
        return StringClassification(cls, confidence=1.0, method="structural")
    
    # Ступень 2: контекст вызова (O(1))
    if call_context:
        cls = classify_by_call_context(string_val, *call_context)
        if cls and cls != "SERVICE_URL_OR_PATH":
            return StringClassification(cls, confidence=0.95, method="context")
    
    # Ступень 3: regex по форме строки (O(n_patterns))
    for cls, patterns in STRING_CLASS_PATTERNS.items():
        for p in patterns:
            if p.match(string_val):
                # Regex — низкая уверенность, нужно подтверждение контекстом
                return StringClassification(cls, confidence=0.6, method="regex",
                                            needs_context_confirm=True)
    
    # Ступень 4: векторный поиск (только если embedding_model доступен)
    if embedding_model:
        cls, sim = classify_string_by_vector(string_val, embedding_model, cache)
        return StringClassification(cls, confidence=sim, method="vector")
    
    return StringClassification("UNKNOWN", confidence=0.0, method="none")
```

---

## C. SymbolIndex с FQN-резолвингом статических констант

### Критическая ошибка без FQN

В train-ticket **5 разных файлов** называются `Queues.java` (по одному в каждом сервисе) — все содержат поле `queueName` с разными значениями:

| Сервис | FQN | queueName |
|---|---|---|
| ts-notification-service | `notification.config.Queues` | `"email"` |
| ts-preserve-service | `preserve.config.Queues` | `"email"` |
| ts-preserve-other-service | `preserveOther.config.Queues` | `"food_delivery"` |
| ts-food-service | `foodsearch.config.Queues` | `"email"` |
| ts-delivery-service | `delivery.config.Queues` | `"food_delivery"` |

SymbolIndex без FQN хранит `(class_name, field_name) → value` — все 5 перезаписывают друг друга. **Решение: FQN = package + class + field, resolve через imports файла.**

### StaticFieldEntry с FQN

```python
@dataclass
class StaticFieldEntry:
    fqn: str                       # "notification.config.Queues.queueName"
    class_name: str                # "Queues" (короткое)
    field_name: str                # "queueName"
    package: str                   # "notification.config"
    value: str                     # "email"
    references_class: str | None   # если ссылается на другую константу
    references_field: str | None
```

### SymbolIndex: resolve через imports

```python
class SymbolIndex:
    def __init__(self):
        self._by_fqn: dict[str, StaticFieldEntry] = {}
        self._by_short: dict[tuple[str, str], list[StaticFieldEntry]] = {}

    def add_field(self, entry: StaticFieldEntry):
        self._by_fqn[entry.fqn] = entry
        key = (entry.class_name, entry.field_name)
        self._by_short.setdefault(key, []).append(entry)

    def resolve_ref(
        self,
        cls_ref: str,             # "Queues"
        field: str,               # "queueName"
        imports: dict[str, str],  # short → fqn из imports файла
    ) -> str | None:
        # Попытка 1: через imports файла (точное)
        fqn_cls = imports.get(cls_ref, cls_ref)
        entry = self._by_fqn.get(f"{fqn_cls}.{field}")
        if entry:
            return entry.value

        # Попытка 2: wildcard import (import delivery.config.*)
        for imp_fqn in imports.values():
            if imp_fqn.endswith('.*'):
                entry = self._by_fqn.get(f"{imp_fqn[:-1]}{cls_ref}.{field}")
                if entry:
                    return entry.value

        # Попытка 3: same package (нет импорта — тот же пакет)
        candidates = self._by_short.get((cls_ref, field), [])
        if len(candidates) == 1:
            return candidates[0].value

        # Попытка 4: неоднозначно → bi-encoder реранкер (§H)
        return None
```

### Сбор FQN при парсинге

```python
def index_static_fields(file_path: Path, tree, source_text: str) -> list[StaticFieldEntry]:
    pkg_node = find_package_declaration(tree)
    pkg = source_text[pkg_node.start_byte:pkg_node.end_byte].strip() if pkg_node else ""
    cls_name = get_node_name(find_first_class(tree), source_text)
    fqn_cls = f"{pkg}.{cls_name}" if pkg else cls_name

    entries = []
    for field_node in find_static_string_fields(tree):
        fname = get_field_name(field_node, source_text)
        fval = get_field_string_value(field_node, source_text)
        if fname and fval:
            entries.append(StaticFieldEntry(
                fqn=f"{fqn_cls}.{fname}",
                class_name=cls_name, field_name=fname, package=pkg, value=fval,
            ))
    return entries
```

### Рекурсивный резолвинг

```python
def resolve_static_field_ref(
    cls_ref: str, field: str, imports: dict[str, str],
    symbol_index: SymbolIndex, depth: int = 0, cap: int = 5,
) -> str | None:
    if depth > cap:
        return None
    val = symbol_index.resolve_ref(cls_ref, field, imports)
    if val is None:
        return None
    # Если значение само ссылается на константу (рекурсия)
    m = re.match(r'^(\w+)\.(\w+)$', val)
    if m:
        return resolve_static_field_ref(m.group(1), m.group(2), imports, symbol_index, depth+1, cap)
    return val
```

---

## D. Полный алгоритм разворачивания URL-цепочки

Объединяет: reaching defs (ADR-021) + static field resolve + строковую классификацию.

```python
def resolve_url_at_call(
    call_node,           # tree-sitter узел вызова restTemplate.exchange(...)
    method_node,         # содержащий метод
    source_text: str,
    symbol_index: SymbolIndex,
    embedding_model,
) -> list[ResolvedUrl]:
    """
    Для вызова restTemplate.exchange(ARG0, ...) разворачивает ARG0 в конкретные URL.
    Возвращает список (может быть несколько при условных ветках — Паттерн B).
    """
    arg0 = get_call_arg(call_node, 0, source_text)  # "requestUrl", "USER_SERVICE_IP_URI", "..."
    
    # Строить Value Flow для метода
    flow = build_value_flow(method_node, source_text, symbol_index)
    
    # Разворачиваем ARG0
    return expand_value(arg0, flow, source_text, symbol_index, embedding_model)

def expand_value(
    expr: str,
    flow: dict[str, ValueNode],
    source_text: str,
    index: SymbolIndex,
    emb_model,
    depth: int = 0,
    cap: int = 10,
) -> list[ResolvedUrl]:
    if depth > cap:
        return []
    
    # Случай 1: строковый литерал прямо в выражении
    m = re.match(r'^"([^"]+)"$', expr.strip())
    if m:
        val = m.group(1)
        cls = classify_string(val, call_context=None, embedding_model=emb_model, cache=EMB_CACHE)
        return [ResolvedUrl(url=val, string_class=cls.cls, confidence=cls.confidence)]
    
    # Случай 2: ссылка на переменную в flow
    if expr in flow:
        node = flow[expr]
        if node.value:
            # Однозначное значение
            return [ResolvedUrl(url=node.value, string_class="SERVICE_URL", confidence=0.9)]
        if node.is_conditional and node.possible_values:
            # Условное — несколько возможных значений
            return [ResolvedUrl(url=v, conditional=True, cfg_condition=node.condition)
                    for v in node.possible_values]
    
    # Случай 3: конкатенация A + "/suffix"
    parts = split_concat(expr)  # ["user_service_url", "+", '"/api/v1/users"', "+", '"/"', "+", "userId"]
    if len(parts) > 1:
        base_vals = expand_value(parts[0], flow, source_text, index, emb_model, depth+1, cap)
        suffix = "".join(p.strip('"') for p in parts[1:] if p.startswith('"'))
        return [ResolvedUrl(url=rv.url + suffix, conditional=rv.conditional)
                for rv in base_vals]
    
    # Случай 4: ссылка на статическое поле Class.FIELD
    m = re.match(r'(\w+)\.(\w+)', expr)
    if m:
        val = resolve_static_field_ref(m.group(1), m.group(2), index)
        if val:
            return [ResolvedUrl(url=val, string_class="QUEUE_NAME", confidence=0.95)]
    
    return [ResolvedUrl(url=None, string_class="UNKNOWN", confidence=0.0)]
```

---

## E. Интеграция @Value жизненного цикла

```python
@dataclass
class ValueAnnotation:
    key: str                    # "email_address"
    default: str | None         # "trainticket_notify@163.com"  (после двоеточия)
    field_name: str             # "emailAddr"
    is_shadow: bool             # True если закомментировано (// @Value)

VALUE_ANNOTATION_PATTERN = re.compile(
    r'(?://\s*)?@Value\s*\(\s*["\']?\$\{([^}:]+)(?::([^}]*))?\}["\']?\s*\)'
)

def parse_value_annotations(class_node, source_text: str) -> list[ValueAnnotation]:
    results = []
    for m in VALUE_ANNOTATION_PATTERN.finditer(source_text):
        is_shadow = source_text[max(0, m.start()-5):m.start()].strip().startswith('//')
        results.append(ValueAnnotation(
            key=m.group(1),
            default=m.group(2),
            field_name=extract_next_field_name(m.end(), source_text),
            is_shadow=is_shadow,
        ))
    return results
```

**Резолвинг значения `@Value` — полная lookup chain:**

```
@Value("${key:default}") → resolve(key, service, context):

  1. ENV_INDEX[service][key]              точное совпадение → HIGH conf (1.0)
  2. ENV_INDEX[service][normalize(key)]   нормализация ключа (-→_, .→_) → HIGH conf (0.95)
  3. application.yml[service][key]        из resources/application.yml → HIGH conf (0.95)
  4. quickstart-docker-compose.yml[key]   расширенный compose (ВСЕ compose файлы) → MED conf (0.8)
  5. ENV_INDEX[*][key]                    другой сервис с таким же ключом → MED conf (0.7)
  6. BGE-M3 fuzzy в ENV_INDEX             cosine ≥ 0.80 → MED conf (0.7 × sim)
  7. default из @Value("${key:DEFAULT}")  → LOW conf (0.6)
  8. USES_MAGIC_CALL                      → conf = 0.0

Шаги 5–6 применяются только если ключ не найден в сервисе-источнике.
Шаг 4: искать ВСЕ *docker-compose*.yml в репо через Two-Phase (ADR-022 §A).
```

**Затирание @Value**: если после инъекции есть `this.url = System.getenv("OVERRIDE")` — помечаем поле как O1 (перекрыт из env). Приоритет в ENV_INDEX.

---

## F. Что НЕ создаёт ребро

| Строка / паттерн | Класс | Действие |
|---|---|---|
| `"PAID"`, `"NOTPAID"`, `"CANCEL"` | BUSINESS_STATUS | Игнорировать |
| `"preserve_success.ftl"` | TEMPLATE_NAME | Игнорировать |
| SQL запрос | SQL_QUERY | Игнорировать |
| UUID / random ID | DATA | Игнорировать |
| Строка в тестовом файле (`/test/`) | любой | Игнорировать (ADR-019) |
| logger.info("...") аргумент | LOG_MESSAGE | Игнорировать |
| Exception message `throw new RuntimeException("...")` | ERROR_MSG | Игнорировать |

**Дополнительная проверка для коротких строк** (≤ 20 символов, нет точки/слэша):

```python
SHORT_STRING_AMBIGUOUS = re.compile(r'^[a-z][a-z0-9_]{2,19}$')

def is_potential_queue_name(s: str, call_context: tuple | None) -> bool:
    """
    "email", "food_delivery" — короткие строки нижнего регистра.
    Считаем QUEUE_NAME только если:
    1. Используется как аргумент convertAndSend / @RabbitListener
    2. ИЛИ присвоено static final полю класса с именем *Queue* или *Name*
    """
    if call_context and call_context[0] in ("convertAndSend", "basicPublish"):
        return True
    return False
```

---

## H. Bi-encoder реранкер для неоднозначных ссылок

Применяется когда `SymbolIndex.resolve_ref()` вернул `None` (несколько кандидатов).

```
Шаг 1: Regex генерирует кандидатов
  Берём все известные значения нужного типа:
  - для QUEUE_NAME → все StaticFieldEntry где field_name ≈ "queue*" или "topic*"
  - для SERVICE_NAME → ServiceRegistry
  
Шаг 2: Bi-encoder кодирует запрос и кандидатов
  Запрос = контекст использования:
    "[SVC:ts-food-service][FILE:RabbitSend][METHOD:send][CALL:convertAndSend(Queues.queueName,val)]"
  Кандидаты = контексты объявления:
    "[SVC:ts-food-service][FILE:Queues][FIELD:queueName][VALUE:email]"
    "[SVC:ts-delivery-service][FILE:Queues][FIELD:queueName][VALUE:food_delivery]"

Шаг 3: Cosine similarity → топ-K (K=3)

Шаг 4: Реранкер (эвристики)
  +0.20: same-service (кандидат из того же сервиса)
  +0.15: import-match (кандидат совпадает с import в файле)
```

```python
def rerank_candidates(
    candidates: list[ResolutionCandidate],
    use_context: UseContext,
) -> ResolutionCandidate | None:
    for c in candidates:
        c.rerank_score = c.embedding_sim
        if c.source_service == use_context.service:
            c.rerank_score += 0.20
        if c.source_fqn in use_context.imports.values():
            c.rerank_score += 0.15
    candidates.sort(key=lambda x: x.rerank_score, reverse=True)
    best = candidates[0] if candidates else None
    return best if best and best.rerank_score >= 0.65 else None
```

**Результат для train-ticket:** `ts-food-service.convertAndSend(Queues.queueName, ...)` → same-service bonus → `"email"` (не `"food_delivery"` из ts-delivery-service).

---

## I. Аномалии train-ticket (выявлено при тестировании FQN)

| Аномалия | Детали | Действие в графе |
|---|---|---|
| **CIRCULAR**: ts-notification-service на `"email"` | Сервис одновременно шлёт и слушает свою же очередь | Пометить CIRCULAR на ребре; не считать dead message |
| **HARDCODED_IP**: 5 сервисов | `jdbc:mysql://${HOST:10.176.122.1}:3306/ts` — IP разработчика как default | Не создавать SHARES_RESOURCE через IP; `missing_db_config=True`; в `technical_debt[]` |
| **MISSING_CONFIG**: @Value без дефолта и без compose | `${test_send_mail_user}` в ts-notification-service, `${swagger.controllerPackage}` в ts-common | В `technical_debt[MISSING_CONFIG]` |
| **ts-consign-mysql не в корневом compose** | Только в quickstart-compose; `ts-consign-service` не стартует без него | Искать ВСЕ `*docker-compose*.yml` через Two-Phase |

---

## G. Итог: что меняется в пайплайне

| Компонент | До | После |
|---|---|---|
| `convertAndSend(Queues.queueName, val)` | queue_name = "Queues.queueName" | FQN resolve → "email" (правильный сервис) |
| 5 одноимённых `Queues.java` | коллизия, неверное значение | SymbolIndex с FQN + same-service reranker |
| `exchange(USER_SERVICE_IP_URI + "/" + id, ...)` | URL = None | expand_value → "http://ts-user-service/api/v1/users/{id}" |
| `exchange(requestUrl, ...)` (условный) | 1 URL | 2 условных URL с cfg_condition |
| `@Value("${key:default}")` | только key lookup | 8-шаговая lookup chain |
| `"PAID"` в exchange() | создавался CALLS_SERVICE (ошибка) | структурная проверка → BUSINESS_STATUS → игнорировать |
| `"email"` в `sendEmail(mail, "email@test.com")` | потенциальный QUEUE_NAME | контекст sendEmail → TEMPLATE_NAME / DATA |
| Закомментированный `// @Value(...)` | пропускался | is_shadow=True → SHADOW_CALLS_SERVICE conf=0.90 |
| IP `10.176.122.1` как default datasource | SHARES_RESOURCE через IP | не создавать; `missing_db_config=True` |
