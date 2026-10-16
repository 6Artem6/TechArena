# ADR-021: Compiler-inspired анализ, архитектурные слои, технический долг и восстановление при ошибках

**Статус:** Принято (A–C, D.0–D.3: Tier 2–3; D.4 LLM repair: Tier 5)  
**Дата:** 2026-10-13  
**Связанные:** ADR-001, ADR-002, ADR-003, ADR-005, ADR-006, ADR-011, ADR-016, ADR-019  
> Аномалии train-ticket (циклическая очередь, хардкоженные IP, MISSING_CONFIG) задокументированы в **ADR-023 §I**.

---

## Контекст

Предыдущие ADR покрыли парсинг, граф, обход, fallback, эмбеддинги. Остались четыре открытых вопроса:

1. Компилятор недоступен — но его методы применимы к статическому AST  
2. Часть архитектурных данных важна, но не попадает в финальный JSON  
3. Технический долг: когда связь настроена только с одной стороны  
4. Если tree-sitter ломается на файле — нужен градиент восстановления

---

## A. Compiler-inspired методы на статическом коде

### Принцип

Компилятор строит несколько промежуточных представлений из одного исходного файла. Всё это применимо к статическому AST без выполнения кода.

### A.1 Control Flow Graph (CFG)

**Проблема:** `if (tripId.startsWith("G"))` — два разных CALLS_SERVICE. Без CFG создаём лишнее ребро или упускаем нужное.

**Применение:**

```python
@dataclass
class CfgEdge:
    from_stmt: int    # индекс statement в методе
    to_stmt: int
    condition: str | None  # "tripId.startsWith(\"G\")" или None

def build_method_cfg(method_node, source_text: str) -> list[CfgEdge]:
    """
    Строит упрощённый CFG из tree-sitter AST для одного метода.
    Не выполняет код — только структурирует переходы по веткам if/else/for/while.
    """
    edges = []
    stmts = list(method_node.children_by_type("statement"))
    for i, stmt in enumerate(stmts):
        if stmt.type == "if_statement":
            cond_text = source_text[stmt.child_by_field_name("condition").start_byte:
                                    stmt.child_by_field_name("condition").end_byte]
            # then-ветка
            edges.append(CfgEdge(i, i+1, condition=cond_text))
            # else-ветка
            else_node = stmt.child_by_field_name("alternative")
            if else_node:
                edges.append(CfgEdge(i, i+2, condition=f"!({cond_text})"))
        else:
            edges.append(CfgEdge(i, i+1, condition=None))
    return edges
```

**Результат в графе:** CALLS_SERVICE ребро получает атрибут `cfg_condition: "tripId.startsWith('G')"` — показывает в blast radius что ребро условное, не безусловное.

### A.2 Reaching Definitions / Use-Def chains

**Проблема:** `url` присваивается в трёх местах внутри метода — нужно знать, какое значение достигает `restTemplate.exchange(url, ...)`.

**Применение:**

```python
@dataclass
class Def:
    var_name: str
    value_expr: str   # "travel_service_url + \"/api/v1/...\"" 
    stmt_index: int

def reaching_defs_at(method_node, use_stmt_index: int, source_text: str) -> list[Def]:
    """
    Упрощённый reaching definitions: для каждого использования переменной
    находим последнее присваивание на path к этому use в CFG.
    Без выполнения — статический обход AST.
    """
    defs: dict[str, list[Def]] = defaultdict(list)
    for i, stmt in enumerate(iter_stmts(method_node)):
        if i >= use_stmt_index:
            break
        if stmt.type in ("local_variable_declaration", "assignment_expression"):
            var, expr = extract_assignment(stmt, source_text)
            if var:
                defs[var].append(Def(var, expr, i))
    # возвращаем последнее def для каждой переменной
    return [defs_list[-1] for defs_list in defs.values() if defs_list]
```

**Пример из train-ticket:**

```java
String requestUrl = "";
String travel_service_url = getServiceUrl("ts-travel-service");   // def
String travel2_service_url = getServiceUrl("ts-travel2-service"); // def
if (tripId.startsWith("G") || tripId.startsWith("D")) {
    requestUrl = travel_service_url + "/api/v1/...";              // def в ветке
} else {
    requestUrl = travel2_service_url + "/api/v1/...";             // def в ветке
}
restTemplate.exchange(requestUrl, ...);                           // USE
```

→ Reaching defs для `requestUrl` → два значения из двух веток → два условных CALLS_SERVICE.

### A.3 Alias Analysis

**Проблема:** `String serviceUrl = url` — переменная-алиас. Без анализа теряем связь с оригинальным `getServiceUrl(...)`.

**Применение:**

```python
ALIAS_PATTERNS = [
    # String b = a;
    re.compile(r'(?:final\s+)?String\s+(\w+)\s*=\s*(\w+)\s*;'),
    # String b = a + "/suffix";
    re.compile(r'(?:final\s+)?String\s+(\w+)\s*=\s*(\w+)\s*\+'),
]

def resolve_alias(var_name: str, defs: list[Def]) -> str:
    """Рекурсивно разворачивает алиасы до первичного источника (cap=10)."""
    seen = {var_name}
    current = var_name
    for _ in range(10):
        alias_def = next((d for d in defs if d.var_name == current), None)
        if alias_def is None:
            break
        # извлекаем RHS из выражения
        rhs = extract_rhs_var(alias_def.value_expr)
        if rhs is None or rhs in seen:
            break
        seen.add(rhs)
        current = rhs
    return current
```

### A.4 Lightweight Type Inference

**Проблема:** `@Autowired OrderService orderService` — нужно найти `OrderServiceImpl implements OrderService`, чтобы добавить реальное ребро вместо CALLS на интерфейс.

**Применение через SymbolIndex:**

```python
def resolve_autowired_type(interface_fqn: str, index: SymbolIndex) -> str | None:
    """
    Без выполнения: ищем класс, который implements interface_fqn.
    Возвращает единственную реализацию или None если неоднозначно.
    """
    implementors = index.get_implementors(interface_fqn)
    if len(implementors) == 1:
        return implementors[0]
    if len(implementors) > 1:
        # Неоднозначность — не угадываем, создаём UnknownDynamicTarget
        return None
    # Попытка найти по соглашению именования: OrderService → OrderServiceImpl
    candidate = interface_fqn.replace("Service", "ServiceImpl")
    if index.has_class(candidate):
        return candidate
    return None
```

**Результат:** CALLS ребро к `OrderServiceImpl` вместо к интерфейсу `OrderService`.

### Итог: что добавляем в граф

| Техника | Результат в графе | Атрибут |
|---|---|---|
| CFG | Условные CALLS_SERVICE | `cfg_condition: "..."` |
| Reaching defs | Точные URL у restTemplate вызовов | встроено в CALLS_SERVICE |
| Alias analysis | Разворачивает псевдонимы переменных | прозрачно |
| Type inference | CALLS к Impl вместо Interface | `resolved_from: "autowired_inference"` |

---

## B. Архитектурно важные данные вне финального JSON

### Принцип

Эти данные **извлекаем и храним в метаданных узла/ребра**, но в финальный `blast_radius` JSON они входят только как предупреждения (`warnings[]`), не как основные поля. Игнорировать их нельзя — они влияют на критичность и корректность обхода.

### B.1 @Transactional — границы транзакций

```python
TRANSACTIONAL_PATTERN = re.compile(
    r'@Transactional(?:\(.*?propagation\s*=\s*Propagation\.(\w+).*?\))?'
)

# Что сохраняем:
node.attrs["transactional"] = True
node.attrs["tx_propagation"] = "REQUIRED"  # или REQUIRES_NEW, NESTED, ...
node.attrs["tx_rollback_on"] = ["Exception"]  # если указано rollbackFor=...
```

**Влияние на blast radius:**  
Метод с `@Transactional(propagation=REQUIRES_NEW)` изолирован — его откат не затрагивает вызывающий метод. Это уменьшает blast radius через это ребро.

### B.2 @Async — изоляция потоков

```python
# Из train-ticket: @Async("mySimpleAsync") в ts-inside-payment-service
ASYNC_PATTERN = re.compile(r'@Async(?:\("([^"]+)"\))?')

node.attrs["async_executor"] = "mySimpleAsync"  # или None для default
```

**Влияние:** Ребро CALLS → метод с `@Async` становится ASYNC_CALL с пониженным весом (0.6 вместо 1.0) — сбой асинхронного метода не синхронно обрушивает caller.

### B.3 @PostConstruct — startup зависимости

```python
# Пример из train-ticket: ts-auth-service, JWTProvider.init() — инициализация secretKey
node.attrs["post_construct"] = True
```

**Влияние:** Ошибка в `@PostConstruct` — сервис не поднимется. Добавляем в warnings если изменён файл, содержащий `@PostConstruct`.

### B.4 Spring Events (ApplicationEventPublisher / @EventListener)

```python
EVENT_PUBLISH_PATTERN = re.compile(
    r'\.publishEvent\s*\(\s*new\s+(\w+)\s*\('
)
EVENT_LISTEN_PATTERN = re.compile(
    r'@EventListener(?:\s+public\s+\w+\s+\w+\s*\(\s*(\w+)\s+\w+\))?'
)
```

**В граф:** ASYNC_MESSAGE ребро с `metadata.event_type = "OrderCreatedEvent"` — Spring Events семантически эквивалентны RabbitMQ для blast radius.

### B.5 @Scheduled — периодические задачи

```python
SCHEDULED_PATTERN = re.compile(r'@Scheduled\((?:fixedRate|cron)\s*=\s*["\']?([^,"\')\s]+)')

node.attrs["scheduled"] = True
node.attrs["scheduled_expr"] = "0 * * * * *"
```

**Влияние:** Scheduled метод — отдельная точка входа. Изменение его зависимостей имеет blast radius не меньше чем у @RequestMapping.

### B.6 @HystrixCommand / Resilience4j circuit breakers

```python
CIRCUIT_BREAKER_PATTERNS = [
    re.compile(r'@HystrixCommand\(fallbackMethod\s*=\s*"([^"]+)"'),
    re.compile(r'@CircuitBreaker\(name\s*=\s*"([^"]+)"'),
]

edge.attrs["circuit_breaker"] = True
edge.attrs["fallback_method"] = "getOrderFallback"
```

**Влияние:** CALLS через circuit breaker → меньший blast radius при сбое (есть fallback), но нужно включить fallback-метод в анализ.

### B.7 @Cacheable / @CacheEvict

```python
CACHE_PATTERNS = [
    re.compile(r'@Cacheable\(.*?value\s*=\s*"([^"]+)"'),
    re.compile(r'@CacheEvict\(.*?value\s*=\s*"([^"]+)"'),
]

node.attrs["cache_read"] = "orderCache"   # @Cacheable
node.attrs["cache_evict"] = "orderCache"  # @CacheEvict
```

**Влияние:** Изменение закешированного метода → blast radius включает все потребители кэша (SHARES_RESOURCE ребро через кэш-имя).

### B.8 @ExceptionHandler / @ControllerAdvice

```python
# @ControllerAdvice — глобальный обработчик ошибок
# Изменение GlobalExceptionHandler влияет на все контроллеры
node.attrs["global_exception_handler"] = True
# → добавляем ребро CONTAINS от каждого @RestController к этому узлу
```

### Итог: что попадает в финальный JSON

```json
{
  "blast_radius": [...],
  "warnings": [
    {
      "type": "TRANSACTIONAL_BOUNDARY",
      "node": "ts-order-service/OrderServiceImpl.createOrder",
      "detail": "REQUIRES_NEW isolates rollback — downstream blast may be lower"
    },
    {
      "type": "ASYNC_CALL",
      "edge": "InsidePaymentService → AsyncTask",
      "detail": "Call is @Async — failure is non-blocking for caller"
    },
    {
      "type": "INCOMPLETE_ANALYSIS",
      "node": "ts-order-service/OrderServiceImpl",
      "detail": "has_incomplete_deps=true (comment-detected)"
    }
  ]
}
```

---

## C. Детекция технического долга

### Принцип

Технический долг — когда связь настроена **только с одной стороны**. Система работает, но хрупко: сбой незаметен статически.

### C.1 Dangling Caller (вызов без обработчика)

**Определение:** CALLS_SERVICE ребро существует, но в RouteIndex нет handler для этого пути.

```python
def find_dangling_callers(graph: nx.MultiDiGraph, route_index: RouteIndex) -> list[DebtItem]:
    items = []
    for u, v, data in graph.edges(data=True):
        if data["type"] != "CALLS_SERVICE":
            continue
        url = data.get("url")
        method = data.get("http_method", "GET")
        if url and not route_index.has_handler(url, method):
            items.append(DebtItem(
                kind="DANGLING_CALLER",
                source_node=u,
                target_url=url,
                detail=f"No handler found for {method} {url}"
            ))
    return items
```

### C.2 Orphan Endpoint (обработчик без вызывающих)

**Определение:** `@RequestMapping` зарегистрирован в RouteIndex, но ни один узел графа не указывает на него CALLS_SERVICE ребром.

```python
def find_orphan_endpoints(graph: nx.MultiDiGraph, route_index: RouteIndex) -> list[DebtItem]:
    # Собираем все URL которые вызываются
    called_urls: set[str] = set()
    for _, _, data in graph.edges(data=True):
        if data["type"] == "CALLS_SERVICE" and data.get("url"):
            called_urls.add(normalize_url(data["url"]))
    
    items = []
    for route in route_index.all_routes():
        if normalize_url(route.path) not in called_urls:
            # Исключение: gateway-маршрут (вызывается извне)
            if not route_index.is_gateway_exposed(route.path):
                items.append(DebtItem(
                    kind="ORPHAN_ENDPOINT",
                    source_node=route.handler_node,
                    target_url=route.path,
                    detail="Endpoint has no known callers within the repo"
                ))
    return items
```

### C.3 Dead Message (очередь без потребителя)

**Определение:** `AmqpTemplate.convertAndSend("queue_name", ...)` без соответствующего `@RabbitListener(queues="queue_name")`.

```python
def find_dead_messages(graph: nx.MultiDiGraph) -> list[DebtItem]:
    published: dict[str, str] = {}   # queue → publisher_node
    consumed: set[str] = set()
    
    for node, data in graph.nodes(data=True):
        for q in data.get("publishes_queues", []):
            published[q] = node
        for q in data.get("consumes_queues", []):
            consumed.add(q)
    
    return [
        DebtItem(
            kind="DEAD_MESSAGE",
            source_node=published[q],
            detail=f"Queue '{q}' published but no @RabbitListener found"
        )
        for q in published if q not in consumed
    ]
```

### C.4 Dead Service Reference

**Определение:** Сервис вызывается через URL (restTemplate, FeignClient, AsyncTask), но его имя:
- НЕ присутствует в docker-compose как service name
- НЕ в getServiceUrl() ключах
- НЕ в Kubernetes/Helm service names

**Реальный пример из train-ticket:**

```java
// ts-inside-payment-service/async/AsyncTask.java:27
restTemplate.getForObject("http://rest-service-external:16100/greet", Boolean.class)
// → "rest-service-external" нет в docker-compose.yml
// → нет в getServiceUrl() вызовах
// → ПОДТВЕРЖДЁННЫЙ dead service reference
```

```python
def find_dead_service_refs(graph: nx.MultiDiGraph,
                            known_services: set[str]) -> list[DebtItem]:
    items = []
    for _, _, data in graph.edges(data=True):
        if data["type"] not in ("CALLS_SERVICE", "ASYNC_MESSAGE"):
            continue
        target = data.get("target_service")
        if target and target not in known_services:
            items.append(DebtItem(
                kind="DEAD_SERVICE_REF",
                source_node=data.get("source_node"),
                detail=f"Service '{target}' not found in docker-compose or getServiceUrl()",
                confidence=0.9
            ))
    return items
```

### C.5 Missing Config (@Value key не в ENV_INDEX)

```python
def find_missing_configs(graph: nx.MultiDiGraph,
                          env_index: EnvIndex) -> list[DebtItem]:
    items = []
    for node, data in graph.nodes(data=True):
        for key in data.get("value_annotations", []):
            # ${station-service.url} → "station-service.url"
            clean_key = key.strip("${}").split(":")[0]
            if not env_index.has_key(clean_key):
                items.append(DebtItem(
                    kind="MISSING_CONFIG",
                    source_node=node,
                    detail=f"@Value key '{clean_key}' not found in any config source"
                ))
    return items
```

### Хранение и вывод

```python
@dataclass
class DebtItem:
    kind: str          # DANGLING_CALLER / ORPHAN_ENDPOINT / DEAD_MESSAGE / 
                       # DEAD_SERVICE_REF / MISSING_CONFIG
    source_node: str
    target_url: str | None = None
    detail: str = ""
    confidence: float = 1.0
    confirmed_in_repo: bool = False  # True только если явно верифицировано

# В JSON-выводе:
{
  "technical_debt": [
    {
      "kind": "DEAD_SERVICE_REF",
      "node": "ts-inside-payment-service/async/AsyncTask",
      "detail": "Service 'rest-service-external:16100' not in docker-compose",
      "confirmed": true
    }
  ]
}
```

### Граница detection/inference

Технический долг **не влияет на IoU метрику напрямую** — он не меняет blast_radius набор. Но:
- Dead service ref → не создавать CALLS_SERVICE ребро (иначе blast radius уходит в несуществующий узел)
- Orphan endpoint → не включать в обратный BFS (нет вызывающих)

---

## D. Градиент восстановления при ошибках tree-sitter

### Принцип

tree-sitter создаёт ERROR-узлы вместо краша. Нужна стратегия от мягкого к жёсткому.

### D.0 Baseline: измерение масштаба

```python
def count_error_nodes(tree) -> tuple[int, int]:
    """Возвращает (error_count, total_nodes)."""
    errors = 0
    total = 0
    for node in traverse_tree(tree):
        total += 1
        if node.type == "ERROR" or node.is_missing:
            errors += 1
    return errors, total

def error_ratio(tree) -> float:
    errors, total = count_error_nodes(tree)
    return errors / max(total, 1)
```

**Пороги:**
- `ratio < 0.01` → файл чистый, обрабатывать нормально
- `0.01 ≤ ratio < 0.10` → мягкое восстановление (D.1)
- `0.10 ≤ ratio < 0.30` → агрессивное восстановление (D.2)
- `ratio ≥ 0.30` → fallback на альтернативный парсер (D.3) или LLM (D.4)

**Из опыта train-ticket:** 0 ERROR-узлов на 401 Java файлах → уровень D.0, стратегии D.1–D.3 резервные для нестандартных файлов, D.4 (LLM repair) — Tier 5.

### D.1 Мягкое восстановление: хирургическое удаление строк

**Идея:** Найти строку с ERROR-узлом, убрать её, перепарсить оставшееся.

```python
def recover_by_line_removal(source: str, tree, language) -> tuple[str, str]:
    """
    Находит первую строку с ERROR-узлом, убирает её.
    Возвращает (recovered_source, removed_line).
    """
    for node in traverse_tree(tree):
        if node.type == "ERROR":
            start_line = node.start_point[0]
            lines = source.splitlines(keepends=True)
            removed = lines[start_line]
            recovered = "".join(lines[:start_line] + lines[start_line+1:])
            # Перепарсить и проверить ratio
            new_tree = language.parser().parse(recovered.encode())
            new_ratio = error_ratio(new_tree)
            return recovered, removed
    return source, ""
```

### D.2 Агрессивное восстановление: бинарная биссекция

**Идея:** Разбить файл пополам, определить какая половина проблемная.

```python
def recover_by_bisection(source: str, language, max_iterations: int = 8) -> str:
    """
    Бинарная биссекция: находит минимальный проблемный блок (функцию/класс).
    Возвращает источник без проблемного блока.
    """
    lines = source.splitlines(keepends=True)
    lo, hi = 0, len(lines)
    
    for _ in range(max_iterations):
        mid = (lo + hi) // 2
        candidate = "".join(lines[:mid])
        tree = language.parser().parse(candidate.encode())
        if error_ratio(tree) < 0.01:
            lo = mid  # первая половина чистая, проблема в hi
        else:
            hi = mid  # проблема в начале
    
    # Убираем проблемный диапазон [hi, len]
    recovered = "".join(lines[:lo] + lines[hi:])
    return recovered
```

### D.3 Альтернативный парсер (javalang)

**Применяется когда:** tree-sitter не справляется, но файл — Java.

```python
def parse_with_javalang_fallback(source: str) -> list[MinimalClassNode]:
    """
    javalang не поддерживает некоторые новые Java конструкции,
    но хорошо работает с Java 8 — основным уровнем train-ticket.
    """
    try:
        import javalang
        tree = javalang.parse.parse(source)
        return extract_minimal_from_javalang(tree)
    except javalang.parser.JavaSyntaxError as e:
        logger.warning(f"javalang also failed: {e}")
        return []
```

### D.4 LLM repair (крайний случай) — Tier 5

**Tier:** 5 (Deferred — только если все остальные стратегии не справились и файл критичен)  
**Альтернатива в Tier 3:** javalang fallback (D.3) покрывает 99% случаев train-ticket. LLM repair не нужен для хакатонного базового набора.

**Применяется когда:** Ни tree-sitter, ни javalang не справились, файл архитектурно важен.

```python
REPAIR_PROMPT = """
Fix only the syntax errors in this Java file so it can be parsed.
Do not change logic, method names, or structure.
Return ONLY the corrected code, no explanation.

FILE:
{broken_source}
"""

def repair_with_llm(source: str, ollama_client) -> str | None:
    """
    LLM repair только для Ollama (local-first!).
    Никогда не вызывать облачные LLM.
    Лимит: MAX_LLM_REPAIRS = 3 per indexing run.
    """
    if not ollama_client.is_available():
        return None
    response = ollama_client.generate(
        model="qwen2.5-coder:7b",
        prompt=REPAIR_PROMPT.format(broken_source=source[:8000]),  # обрезаем большие файлы
        options={"temperature": 0.0}
    )
    repaired = response.get("response", "").strip()
    # Верифицируем: перепарсить и убедиться что errors = 0
    tree = JAVA_LANGUAGE.parser().parse(repaired.encode())
    if error_ratio(tree) < 0.01:
        return repaired
    return None
```

**Ограничения LLM repair:**
- Только local Ollama — строгое local-first
- `MAX_LLM_REPAIRS = 3` на индексирование (дорого по времени)
- Только если файл в critical path (сервис-корень или архитектурно значимый)
- Результат кешируется в `indexed_files` с флагом `llm_repaired=True`

### D.5 Полный градиент

```python
def parse_with_recovery(file_path: Path, language) -> ParseResult:
    source = file_path.read_text(encoding="utf-8", errors="replace")
    tree = language.parser().parse(source.encode())
    ratio = error_ratio(tree)
    
    if ratio < 0.01:
        return ParseResult(tree=tree, source=source, strategy="CLEAN")
    
    if ratio < 0.10:
        recovered, removed = recover_by_line_removal(source, tree, language)
        tree2 = language.parser().parse(recovered.encode())
        if error_ratio(tree2) < 0.01:
            return ParseResult(tree=tree2, source=recovered, strategy="LINE_REMOVED",
                               removed_lines=[removed])
    
    if ratio < 0.30:
        recovered = recover_by_bisection(source, language)
        tree3 = language.parser().parse(recovered.encode())
        if error_ratio(tree3) < 0.05:
            return ParseResult(tree=tree3, source=recovered, strategy="BISECTION")
    
    # Попытка javalang (только Java)
    if file_path.suffix == ".java":
        minimal = parse_with_javalang_fallback(source)
        if minimal:
            return ParseResult(minimal_nodes=minimal, strategy="JAVALANG_FALLBACK")
    
    # LLM repair: только если файл critical
    if is_critical_file(file_path) and ollama_available():
        repaired = repair_with_llm(source, OLLAMA_CLIENT)
        if repaired:
            tree4 = language.parser().parse(repaired.encode())
            return ParseResult(tree=tree4, source=repaired, strategy="LLM_REPAIRED")
    
    # Финальный fallback: MinimalClassNode по имени файла
    return ParseResult(
        minimal_nodes=[MinimalClassNode(file_path=file_path, parse_status="FAILED")],
        strategy="MINIMAL_FALLBACK"
    )
```

### Атрибуты в узле после восстановления

```python
node.attrs["parse_strategy"] = "LINE_REMOVED"     # CLEAN / LINE_REMOVED / BISECTION /
                                                   # JAVALANG_FALLBACK / LLM_REPAIRED / MINIMAL_FALLBACK
node.attrs["parse_confidence"] = 0.8               # 1.0=CLEAN, 0.8=LINE_REMOVED, 
                                                   # 0.6=BISECTION, 0.4=MINIMAL_FALLBACK
```

`parse_confidence` влияет на criticality: `criticality *= parse_confidence`.

---

## Итог: что добавляется в граф и вывод

| Источник | Атрибут/ребро | Попадает в JSON |
|---|---|---|
| CFG condition | `edge.cfg_condition` | нет (внутри) |
| Reaching defs | точные URLs у CALLS_SERVICE | да (через edge) |
| @Async | `node.async_executor`, edge weight 0.6 | warnings[] |
| @Transactional | `node.transactional`, `tx_propagation` | warnings[] |
| @Scheduled | `node.scheduled` | warnings[] |
| Circuit breaker | `edge.circuit_breaker`, `fallback_method` | warnings[] |
| Spring Events | ASYNC_MESSAGE ребро | blast_radius |
| @Cacheable | SHARES_RESOURCE через кэш | blast_radius |
| Dead service ref | DebtItem | technical_debt[] |
| Orphan endpoint | DebtItem | technical_debt[] |
| Dead message | DebtItem | technical_debt[] |
| Missing config | DebtItem | technical_debt[] |
| parse_strategy | `node.parse_confidence` → criticality | нет (внутри) |
