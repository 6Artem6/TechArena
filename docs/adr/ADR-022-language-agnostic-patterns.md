# ADR-022: Language-Agnostic Pattern Framework, Framework Profiles, Two-Phase Discovery, Embeddings as Fallback

**Статус:** Принято  
**Дата:** 2026-10-13  
**Связанные:** ADR-001, ADR-011, ADR-018, ADR-020, ADR-021  
**Поглощает:** ADR-017 (Framework Profiles + Auto-Rule Loop → объединено сюда; авто-правила Ollama вынесены в ADR-014 Tier 4)

---

## Контекст

Три проблемы, которые нужно решить совместно:

1. **Регулярные выражения из ADR-021** (аннотации, CFG-паттерны) написаны под Java — при переходе на Python/Go/Node.js они не сработают. Нужен fallback.
2. **Поиск "в лоб"** — грепать весь проект по каждому паттерну дорого и создаёт шум. Нужна тактика "найди нужный участок, потом ищи конкретное".
3. **Конфиги в коде** (Kafka topic, datasource host, queue name) — разбросаны по файлам без схемы. Те же два пункта выше.

---

## A. Two-Phase Discovery — тактика "не в лоб"

### Принцип

```
Фаза 1: File/Class Localization  — найти КАНДИДАТОВ (файлы, классы, методы)
Фаза 2: Detail Extraction        — извлечь КОНКРЕТНОЕ из кандидатов
```

Никогда не запускать Фазу 2 по всему репо. Всегда ограничивать область поиска через Фазу 1.

### Примеры применения

#### Kafka топики

```
Фаза 1: Найти файлы с Kafka
  - по имени: *KafkaConfig*, *KafkaConsumer*, *KafkaProducer*
  - по импорту: grep "import.*kafka" --include="*.java"
  - по application.yml: grep "spring.kafka" в ресурсах
  → candidates: список файлов/классов

Фаза 2: В кандидатах искать детали
  - kafkaTemplate.send("topic-name", ...)
  - @KafkaListener(topics = "topic-name")
  - bootstrap-servers: значение → ServiceRegistry
```

#### datasource host для SHARES_RESOURCE

```
Фаза 1: Найти application.yml файлы
  - по пути: **/resources/application*.yml
  → candidates: все application.yml в репо (~ N сервисов)

Фаза 2: В кандидатах искать datasource.url
  - jdbc:mysql://HOST:PORT/DB
  - mongodb://HOST:PORT/DB
  → извлечь HOST → SHARES_RESOURCE группировка по одинаковому HOST
```

#### nginx proxy_pass

```
Фаза 1: Найти nginx конфиги
  - по имени: nginx.conf, *.nginx, nginx/*.conf
  - по содержимому первой строки: "worker_processes"
  → candidates

Фаза 2: В кандидатах искать proxy_pass
  - location /prefix/ { proxy_pass http://SERVICE:PORT; }
  → CALLS_SERVICE ребро
```

#### Archived/commented конфиги (shadow)

```
Фаза 1: Найти файлы с закомментированными блоками конфигурации
  - по паттерну в коде: строки начинающиеся с #  в *.yml или // в *.java
  - фильтр: только если содержат ключевые слова (rabbitmq|kafka|datasource|@Value)
  → candidates

Фаза 2: Классифицировать закомментированное
  - полный закомментированный блок → shadow connection (ADR-016)
  - одиночная строка → URL hint или TODO (ADR-020)
```

### Two-Phase как универсальный паттерн

```python
class TwoPhaseDiscovery:
    """
    Общий паттерн для любого поиска конфигов/паттернов.
    Фаза 1 использует дешёвые операции (glob, grep по именам файлов).
    Фаза 2 применяет дорогие операции (tree-sitter, regex, эмбеддинги) только к кандидатам.
    """

    def __init__(self, phase1_locator: Callable, phase2_extractor: Callable):
        self.localize = phase1_locator
        self.extract = phase2_extractor

    def run(self, repo_root: Path) -> list[Any]:
        candidates = self.localize(repo_root)   # быстро, грубо
        results = []
        for candidate in candidates:
            try:
                results.extend(self.extract(candidate))   # точно, только по кандидатам
            except Exception:
                pass  # изоляция ошибок (ADR-019)
        return results

# Пример применения:
kafka_discovery = TwoPhaseDiscovery(
    phase1_locator=lambda root: find_files_by_import(root, "org.springframework.kafka"),
    phase2_extractor=extract_kafka_topics_from_file
)
```

---

## B. Language-Agnostic Concept Map

### Принцип

Вместо language-specific regex — сначала определяем **концепт** (HTTP endpoint, async call, DB access, config inject), потом диспатчим на language-specific детектор.

```python
@dataclass
class ConceptDetector:
    concept: str               # "http_endpoint", "async_call", "config_inject", ...
    language: str              # "java", "python", "go", "nodejs"
    method: str                # "regex", "tree_sitter", "embedding_fallback"
    pattern: str | None        # для regex/ts-query
    embedding_examples: list[str] | None  # для embedding_fallback
```

### Таблица концептов по языкам

| Концепт | Java | Python | Go | Node.js |
|---|---|---|---|---|
| **HTTP endpoint** | `@RequestMapping`, `@Get/PostMapping` | `@app.route`, `@bp.route` | `router.Handle`, `http.HandleFunc` | `app.get`, `router.post` |
| **Service class** | `@Service`, `@Component` | класс в `services/`, `service.py` | struct с `Handler` suffix | `module.exports`, `class *Service` |
| **Async execution** | `@Async`, `CompletableFuture` | `asyncio`, `celery.task`, `@shared_task` | `go func()`, goroutine | `async/await`, `Promise`, worker thread |
| **DB access** | `@Repository`, `JpaRepository`, `JdbcTemplate` | `SQLAlchemy`, `psycopg2`, `pymongo` | `gorm`, `database/sql` | `mongoose`, `sequelize`, `knex` |
| **Config inject** | `@Value("${key}")` | `os.environ.get("KEY")`, `os.getenv` | `os.Getenv("KEY")`, `viper.Get` | `process.env.KEY` |
| **Message send** | `AmqpTemplate.send`, `KafkaTemplate.send` | `pika.channel.publish`, `celery.apply_async` | `amqp.Channel.Publish` | `channel.publish`, `producer.send` |
| **Message receive** | `@RabbitListener`, `@KafkaListener` | `@app.task`, `channel.basic_consume` | `channel.Consume` | `channel.consume`, `consumer.run` |
| **HTTP client** | `RestTemplate`, `FeignClient`, `WebClient` | `requests.get/post`, `urllib.request` | `http.Get`, `http.NewRequest` | `axios.get`, `fetch`, `node-fetch` |
| **Circuit breaker** | `@HystrixCommand`, `@CircuitBreaker` | `pybreaker.CircuitBreaker` | `sony/gobreaker` | `opossum` |
| **Transaction** | `@Transactional` | `db.begin()`/`db.commit()` контекст | `tx.Begin()` | `transaction()` в ORM |
| **Cache** | `@Cacheable`, `@CacheEvict` | `@cache`, `functools.lru_cache` | `bigcache`, `go-cache` | `node-cache`, `ioredis` |
| **Scheduled** | `@Scheduled` | `@celery.beat_schedule`, APScheduler | `time.Ticker`, `robfig/cron` | `node-cron`, `setInterval` |

### ConceptRegistry

```python
CONCEPT_REGISTRY: dict[str, dict[str, ConceptDetector]] = {
    "http_endpoint": {
        "java": ConceptDetector(
            concept="http_endpoint", language="java", method="tree_sitter",
            pattern='(annotation (identifier) @ann (#match? @ann "RequestMapping|GetMapping|PostMapping|PutMapping|DeleteMapping"))'
        ),
        "python": ConceptDetector(
            concept="http_endpoint", language="python", method="regex",
            pattern=r'@(?:app|bp|router)\.(?:route|get|post|put|delete|patch)\s*\(["\']([^"\']+)["\']'
        ),
        "go": ConceptDetector(
            concept="http_endpoint", language="go", method="regex",
            pattern=r'(?:router|mux|r)\s*\.(?:Handle|HandleFunc|GET|POST|PUT|DELETE)\s*\(\s*["\']([^"\']+)["\']'
        ),
        "nodejs": ConceptDetector(
            concept="http_endpoint", language="nodejs", method="regex",
            pattern=r'(?:app|router)\s*\.(?:get|post|put|delete|patch)\s*\(\s*["\']([^"\']+)["\']'
        ),
    },
    "config_inject": {
        "java": ConceptDetector(
            concept="config_inject", language="java", method="regex",
            pattern=r'@Value\s*\(\s*["\']?\$\{([^}:]+)(?::[^}]*)?\}["\']?\s*\)'
        ),
        "python": ConceptDetector(
            concept="config_inject", language="python", method="regex",
            pattern=r'os\.(?:environ\.get|getenv)\s*\(\s*["\']([A-Z_][A-Z0-9_]+)["\']'
        ),
        "go": ConceptDetector(
            concept="config_inject", language="go", method="regex",
            pattern=r'os\.Getenv\s*\(\s*"([A-Z_][A-Z0-9_]+)"'
        ),
        "nodejs": ConceptDetector(
            concept="config_inject", language="nodejs", method="regex",
            pattern=r'process\.env\.([A-Z_][A-Z0-9_]+)'
        ),
    },
    # ... остальные концепты
}
```

### Детекция языка файла

```python
LANG_BY_EXTENSION = {
    ".java": "java",
    ".py": "python",
    ".go": "go",
    ".js": ".ts": "nodejs",
}

def detect_file_language(path: Path) -> str:
    return LANG_BY_EXTENSION.get(path.suffix, "unknown")
```

---

## C. Embeddings as Fallback

### Принцип

Каждый regex/ts-query из ConceptRegistry имеет fallback через BGE-M3 embeddings. Применяется когда:
1. Язык неизвестен или нет детектора для него
2. Regex/ts-query не нашёл ни одного совпадения (возможно нестандартное именование)
3. `error_ratio` файла высокий (tree-sitter не справился — ADR-021 §D)

### Embedding примеры для каждого концепта

```python
CONCEPT_EMBEDDING_EXAMPLES: dict[str, list[str]] = {
    "http_endpoint": [
        "@RequestMapping(\"/api/v1/orderservice/order\")",
        "@app.route('/api/v1/travel', methods=['GET'])",
        "router.Handle(\"/api/v1/station\", handler)",
        "app.get('/api/v1/booking', (req, res) => {})",
        "@RestController public class OrderController",
    ],
    "async_call": [
        "@Async(\"mySimpleAsync\") public void processAsync()",
        "asyncio.create_task(send_notification())",
        "go func() { processEvent(msg) }()",
        "await processOrder(orderId)",
        "CompletableFuture.supplyAsync(() -> fetchData())",
    ],
    "db_access": [
        "@Repository public interface OrderRepository extends JpaRepository",
        "db.session.query(Order).filter_by(id=order_id).first()",
        "db.QueryRow(\"SELECT * FROM orders WHERE id = ?\", id)",
        "Order.findOne({ orderId: id })",
    ],
    "config_inject": [
        "@Value(\"${station-service.url}\") private String stationUrl;",
        "station_url = os.getenv('STATION_SERVICE_URL', 'http://localhost')",
        "stationURL := os.Getenv(\"STATION_SERVICE_URL\")",
        "const stationUrl = process.env.STATION_SERVICE_URL",
    ],
    "message_send": [
        "amqpTemplate.convertAndSend(\"email\", notifyInfo)",
        "kafkaTemplate.send(\"order-created\", message)",
        "channel.basic_publish(exchange='', routing_key='email', body=msg)",
        "producer.send({ topic: 'order-created', messages: [{ value: msg }] })",
    ],
}
```

### Pipeline: regex → embedding fallback

```python
def detect_concept_in_file(
    file_path: Path,
    concept: str,
    source_text: str,
    tree,
    embedding_model,
) -> list[ConceptMatch]:
    lang = detect_file_language(file_path)
    detector = CONCEPT_REGISTRY.get(concept, {}).get(lang)

    # Путь 1: есть детектор для языка
    if detector:
        if detector.method == "regex":
            matches = list(re.finditer(detector.pattern, source_text))
        elif detector.method == "tree_sitter":
            matches = run_ts_query(tree, detector.pattern, source_text)
        
        if matches:
            return [ConceptMatch(concept=concept, language=lang,
                                 method=detector.method, match=m) for m in matches]

    # Путь 2: Embedding fallback (нет детектора ИЛИ regex нашёл 0 совпадений)
    examples = CONCEPT_EMBEDDING_EXAMPLES.get(concept, [])
    if not examples or embedding_model is None:
        return []

    # Разбиваем файл на chunks (функции/методы через tree-sitter или по строкам)
    chunks = split_into_chunks(source_text, tree, chunk_size=10)
    example_vecs = embedding_model.encode(examples)    # кешируются
    chunk_vecs = embedding_model.encode([c.text for c in chunks])

    # Косинусное сходство chunk ↔ examples
    threshold = VECTOR_THRESHOLDS.get("CODE_STRUCTURE", 0.88)
    results = []
    for chunk, vec in zip(chunks, chunk_vecs):
        sim = max(cosine_similarity(vec, ev) for ev in example_vecs)
        if sim >= threshold:
            results.append(ConceptMatch(
                concept=concept, language=lang,
                method="embedding_fallback", confidence=sim,
                match=chunk
            ))
    return results
```

### Когда embedding fallback срабатывает на практике

| Сценарий | Пример | Действие |
|---|---|---|
| Нестандартная библиотека | `WebClient` вместо `RestTemplate` | Embedding находит паттерн HTTP client |
| Другой язык без детектора | Новый репо на Kotlin/Scala | Embedding по примерам JVM |
| Файл с ошибками (JAVALANG_FALLBACK) | Частично сломанный класс | Embedding на доступном тексте |
| Framework magic | `@FeignClient` не распознан | Embedding по сигнатуре интерфейса |
| Alias/wrapper | `class MyRestTemplate extends RestTemplate` | Embedding через похожесть тела класса |

### Кеширование embedding примеров

```python
class ConceptEmbeddingCache:
    """Кешируем эмбеддинги примеров при старте — они не меняются."""
    
    def __init__(self, model):
        self._cache: dict[str, np.ndarray] = {}
        for concept, examples in CONCEPT_EMBEDDING_EXAMPLES.items():
            self._cache[concept] = model.encode(examples, normalize=True)
    
    def get_example_vecs(self, concept: str) -> np.ndarray | None:
        return self._cache.get(concept)
```

Примеры кодируются один раз при инициализации — не при каждом файле.

---

## D. Интеграция с ADR-021: enriched pattern pipeline

Все regex паттерны из ADR-021 (аннотации, CFG, reaching defs) теперь проходят через общий pipeline:

```
ADR-021 паттерн (Java-specific regex)
  ↓  lang == "java" → прямой regex  ✓
  ↓  lang != "java" → ConceptRegistry lookup
       ↓  есть детектор для языка → применить
       ↓  нет детектора → embedding fallback (BGE-M3)
            ↓  sim ≥ threshold → ConceptMatch
            ↓  sim < threshold → не добавлять ребро
```

Пример — детекция HTTP endpoint для Python сервиса `ts-avatar-service`:

```python
# ADR-021 паттерн Java:
# re.compile(r'@(?:Get|Post|Put|Delete|Request)Mapping\s*\(["\']([^"\']+)["\']')
# → не сработает в .py файле

# ConceptRegistry Python:
# r'@(?:app|bp|router)\.(?:route|get|post|put|delete|patch)\s*\(["\']([^"\']+)["\']'
# → найдёт @app.route('/api/v1/avatar/') в ts-avatar-service/avatar/app.py
```

---

## E. Two-Phase для конфигов в коде

Частный случай тактики — конфиги не в yaml файлах, а прямо в коде.

### Паттерн: найти Config-класс, потом извлечь из него

```
Фаза 1: Найти Config-классы
  - по имени класса: *Config, *Configuration, *Properties
  - по аннотации: @Configuration, @ConfigurationProperties
  - по пути: */config/*.java

Фаза 2: В Config-классах искать
  - @Bean методы → зависимости
  - @Value поля → конкретные ключи конфигурации
  - @ConfigurationProperties(prefix="kafka") → Kafka топик/broker конфиги
  - DataSource @Bean → datasource URL → SHARES_RESOURCE
```

```python
class ConfigClassPhase1Locator:
    CONFIG_NAME_PATTERN = re.compile(r'(?:Config|Configuration|Properties)$')
    CONFIG_ANNOTATION = re.compile(r'@(?:Configuration|ConfigurationProperties|EnableKafka|EnableRabbit)')
    
    def locate(self, repo_root: Path) -> list[Path]:
        candidates = []
        for java_file in iter_source_files(repo_root, extensions={".java"}):
            # Быстрая проверка по имени файла
            if self.CONFIG_NAME_PATTERN.search(java_file.stem):
                candidates.append(java_file)
                continue
            # Проверка первых 50 строк на аннотации (дешевле полного парсинга)
            header = java_file.read_text(errors="replace")[:2000]
            if self.CONFIG_ANNOTATION.search(header):
                candidates.append(java_file)
        return candidates
```

### Паттерн: найти `*Properties.java` → извлечь Kafka/RabbitMQ/datasource конфиги

```java
// Типичный Spring @ConfigurationProperties
@ConfigurationProperties(prefix = "spring.kafka")
public class KafkaProperties {
    private String bootstrapServers;
    private String groupId;
}
```

→ Фаза 2: `@ConfigurationProperties(prefix="spring.kafka")` + поля → имена конфиг-ключей → lookup в `application.yml` через Two-Phase из Tier 1.

---

## F. Обновлённый порядок в EnvironmentProfile Tier

Two-Phase применяется на каждом Tier, но глубина Фазы 1 разная:

| Tier | Фаза 1 (локализация) | Фаза 2 (извлечение) |
|---|---|---|
| 1 | Все источники: docker-compose, gateway YAML, nginx.conf, svc.yaml, application.yml | Все детекторы + embedding fallback |
| 2 | application.yml + pom.xml; без docker-compose | Regex детекторы + embedding fallback |
| 3 | Только исходный код: Config-классы + аннотации | Tree-sitter + embedding fallback |
| 4 | Только имена файлов и директорий | Только embedding fallback (нет структурных конфигов) |

---

---

## F. Framework Profiles — декларативные YAML-конфиги фреймворков

### Идея

Вместо хардкода паттернов Java Spring в Python-коде — декларативные YAML-профили. Парсер загружает профиль по детектированному языку/фреймворку. Новый фреймворк = новый YAML, не новый Python-класс.

```
src/infrastructure/framework_profiles/
├── java_spring_boot.yaml       # 47 Java-сервисов train-ticket
├── python_flask.yaml           # ts-avatar-service
├── python_tornado.yaml         # ts-voucher-service
├── go_net_http.yaml            # ts-news-service
└── javascript_express.yaml     # будущие сервисы
```

### Структура профиля (java_spring_boot.yaml)

```yaml
meta:
  id: java_spring_boot
  language: java
  detection:
    pom_dependency: "spring-boot-starter"
    annotation: "@SpringBootApplication"

server:
  route_annotations:
    - name: "@RequestMapping"
      path_attr: ["value", "path"]
      method_attr: "method"
    - name: "@GetMapping"
      path_attr: ["value", "path"]
      method: GET
    - name: "@PostMapping"
      path_attr: ["value", "path"]
      method: POST

client:
  classes:
    - name: RestTemplate
      call_methods: [exchange, getForObject, postForObject, put, delete]
      url_arg_index: 0
    - name: FeignClient
      annotation: "@FeignClient"
      name_attr: name

service_url_helpers:
  - method_name: getServiceUrl
    arg_is_service_name: true
  - method_name: getUrl
    arg_is_service_name: true

dependency_injection:
  field_annotations: ["@Autowired", "@Inject"]
  value_annotation: "@Value"
  value_key_pattern: "\\$\\{([^:}]+)(?::([^}]*))?\\}"

async:
  rabbit_mq:
    sender_class: AmqpTemplate
    sender_method: convertAndSend
    queue_arg_index: 0
    listener_annotation: "@RabbitListener"

anti_edge_patterns:
  - annotation: CommandLineRunner
    reason: seed_data
  - annotation: "@Scheduled"
    reason: internal_task
```

### Профиль python_flask.yaml (ts-avatar-service)

```yaml
meta:
  id: python_flask
  language: python
  detection:
    import: flask

server:
  route_decorators:
    - name: "@app.route"
      path_arg_index: 0
      method_kwarg: methods

client:
  libraries:
    - import: requests
      call_methods: [get, post, put, delete, patch, request]
      url_arg_index: 0
  env_url_pattern:
    function: os.getenv
    fallback_arg_index: 1
```

### Детекция фреймворка

```python
def detect_framework(service_dir: Path) -> str:
    if (service_dir / "pom.xml").exists():
        if "spring-boot-starter" in (service_dir / "pom.xml").read_text():
            return "java_spring_boot"
    py_files = list(service_dir.glob("**/*.py"))
    if py_files:
        first = py_files[0].read_text(errors="replace")
        if "from flask" in first or "import flask" in first:
            return "python_flask"
        if "tornado" in first:
            return "python_tornado"
    if list(service_dir.glob("**/*.go")):
        return "go_net_http"
    return "unknown"
```

---

## G. Роли эмбеддингов (сводная таблица)

BGE-M3 используется для нескольких задач — каждой задаче соответствует своя роль:

| Роль | Модель | Вход | Задача | Tier |
|---|---|---|---|---|
| `ENV_PATTERN` | BGE-M3 | `[TYPE:..] NAME=val` | CALLS_SERVICE / SHARES_RESOURCE / ignore | Tier 3 |
| `SHADOW_CODE` | BGE-M3 | Текст комментария (без маркеров) | код vs текст (бинарная) | Tier 3 |
| `CODE_STRUCTURE` | BGE-M3 | Сигнатура + аннотации | Семантика узла, поиск похожих | Tier 3 |
| `UNKNOWN_PATTERN` | BGE-M3 | Сырой неизвестный паттерн | Кластеризация → auto-rules (ADR-014) | Tier 4 |
| `SEMANTIC_SEARCH` | BGE-M3 | Сервис / роль / summary | Dead-end BFS → SEMANTIC_BRIDGE | Tier 4 |

Примеры seed-векторов для каждой роли — в разделе C (CONCEPT_EMBEDDING_EXAMPLES) и ADR-014.  
Auto-Rule Loop (DBSCAN → Ollama на центроид кластера) описан в **ADR-014 Tier 4**.

---

## Итог: что добавляется в граф и процесс

| Механизм | Что даёт | Новые рёбра/атрибуты |
|---|---|---|
| Framework Profiles (YAML) | Новый язык без рефакторинга Python | Правильные рёбра для всех языков |
| Two-Phase Discovery | Снижает шум; ускоряет анализ | — (организационный) |
| ConceptRegistry | Language-agnostic детекция | Правильные рёбра для Python/Go/Node.js |
| Embedding fallback | Покрывает нестандартный код | ConceptMatch с `method=embedding_fallback`, пониженная confidence |
| nginx.conf parser | Внешняя точка входа | CALLS_SERVICE: nginx → gateway |
| datasource URL | Общие БД | SHARES_RESOURCE через DB host |
| spring.application.name | Нормализация NodeId | Авторитетное имя сервиса |
| Kafka pattern | Async messaging | ASYNC_MESSAGE с broker=kafka |
