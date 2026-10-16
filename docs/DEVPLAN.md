# DEVPLAN — Порядок реализации по приоритету

Тактический план задач: от критичного и простого → к полезному и сложному.  
Архитектурные детали — в ADR. Этот файл — рабочий чеклист.

---

## Tier 1 — Обязательно (MVP, без этого IoU = 0)

| # | Задача | ADR |
|---|---|---|
| 1 | Файловый обходчик: рекурсивный обход, `EXCLUDED_DIRS` (`target`, `node_modules`, `.git`, `__pycache__`), определение языка по расширению | ADR-001, ADR-020 |
| 2 | tree-sitter Java парсер: классы + методы + аннотации (`@RestController`, `@Service`, `@Repository`, `@FeignClient`, `@RequestMapping`, `@Get/Post/Put/DeleteMapping`) + FQN из пакета | ADR-001 |
| 3 | NetworkX граф: типы узлов (`SERVICE`, `CLASS`, `METHOD`, `INTERFACE`, `REPOSITORY`, `ABSTRACT_CLASS`, `LIBRARY`, `DTO`, `CONFIG_SECTION`, `UnknownDynamicTarget`), типы рёбер (`CONTAINS`, `CALLS`, `CALLS_SERVICE`, `USES_DTO`, `IMPLEMENTS`) — REPOSITORY не требует Impl-файла | ADR-002, ADR-029 |
| 4 | NodeId формат + `normalize_node_id()` + aliases — совпадение с gold standard обязательно | ADR-002 |
| 5 | BFS с `TRAVERSAL_POLICY`: `USES_DTO backward=False` (иначе ts-common взрывает граф), `CONTAINS` не считать в depth budget | ADR-005 |
| 6 | JSON вывод в stdout: `affected_nodes[]`, `graph_stats`; все логи строго в stderr | ADR-007 |
| 7 | docker-compose парсинг → `ServiceRegistry` (имена сервисов, порты) + `SHARES_RESOURCE` рёбра | ADR-011 |
| 7b | **SIBLING_OF рёбра** (rule-based): `detect_siblings_by_rules()` — суффикс `-other`, числовой суффикс `2`, `admin-` prefix, `-plan`/`-price`/`-delivery` варианты → вес 0.85/0.6; `split_condition` из CFG (G/D train); depth_cost=0, weight=0.85 | ADR-027 |
| 8 | `getServiceUrl("ts-x")` SymbolIndex: first-pass сбор хелперов по репо, recursive resolve (cap=5) — 279 вызовов в train-ticket | ADR-003, ADR-021 |
| 8b | **PackageIndex**: `build_package_index()` — скан `src/main/java/` → пакетный префикс → сервис; `classify_import()` → external/library/service; disambiguate коллизии `fdse.microservice.*` по субпакету; `edu.fudan.common` → LIBRARY-тип | ADR-029 |
| 8c | **Polyglot URL-резолвинг**: Python `'http://ts-xxx:PORT'` литералы + `os.getenv()` override → `extract_python_service_urls()`; Go/Node.js — проверить env-паттерны, создать CALLS_SERVICE | ADR-029 |
| 9 | **Value Flow Graph** базовый: для каждого метода отслеживать цепочку присваиваний `url = getServiceUrl("x")` → `uri = url + "/path"` → `exchange(uri + "/" + id)` — разворачивать до конкретного URL | ADR-023 |
| 10 | **FQN Static field resolver**: `Queues.queueName` → `"email"` через SymbolIndex с FQN (package+class+field); resolve через imports файла → same-package fallback → bi-encoder реранкер с same-service bonus; без FQN 5 классов `Queues.java` коллидируют → неверные имена очередей | ADR-023 |
| 11 | **Two-Phase Discovery** базовый каркас: `TwoPhaseDiscovery(locator, extractor)` — все конфиг-парсеры строятся на нём, не "в лоб" по всему репо | ADR-022 |
| 12 | **nginx.conf** парсер (Two-Phase): найти `nginx.conf`/`*.nginx` → `proxy_pass` → `CALLS_SERVICE`; `location` → RouteIndex | ADR-011, ADR-022 |
| 13 | **application.yml** расширенный: `spring.application.name` → авторитетное имя сервиса; `spring.datasource.url` → `SHARES_RESOURCE` по DB-хосту; `spring.rabbitmq.host` → `uses_rabbitmq` атрибут | ADR-011 |

---

## Tier 2 — Высокий прирост IoU (сделать до baseline замера)

| # | Задача | ADR |
|---|---|---|
| 12 | gateway YAML → `RouteIndex` (path → service mapping); основной источник межсервисных связей | ADR-011 |
| 13 | `CALLS_SERVICE` рёбра: RestTemplate + URL через `RouteIndex` + regex-паттерн `/api/v1/{service}/` | ADR-003 |
| 14 | `@RequestMapping` → endpoint узлы с path+method; связать с CALLS_SERVICE через RouteIndex | ADR-001, ADR-003 |
| 14b | **SecurityConfig `antMatchers` → RouteIndex с auth-флагами**: `antMatchers(path).permitAll()` → `auth_required=[]`; `antMatchers(path).hasRole("ADMIN")` → `auth_required=["ADMIN"]`; `antMatchers(HttpMethod.POST, path).hasAnyRole(...)` → метод+роль; Swagger paths (`/swagger-ui.html`, `/webjars/**`, `/v2/**`) — пропустить (публичная инфра, одинакова во всех 39 сервисах); `csrf().disable()` + `SessionCreationPolicy.STATELESS` → metadata узла сервиса | ADR-025 §E |
| 14c | **Python `app.run(host, port)` → SERVICE_DEF**: `app.run(host="0.0.0.0", port=N)` → `SERVICE_DEF` с `port=N`; `0.0.0.0` нормализовать в `listen_all` (не hostname); детектировать также `uvicorn.run(..., port=N)`, `socketio.run(..., port=N)` — в Python-сервисах без docker-compose единственный источник порта (пример: ts-avatar-service порт 17001 только в `app.run`) | ADR-029 |
| 15 | FeignClient: `@FeignClient(name="ts-x")` → CALLS_SERVICE ребро без RestTemplate | ADR-003 |
| 16 | RabbitMQ ASYNC_MESSAGE: `AmqpTemplate.convertAndSend` + `@RabbitListener` → ребро с `queue_name` | ADR-003 |
| 17 | Kubernetes `svc.yaml` парсер (Two-Phase): `kind: Service` → `metadata.name` + `spec.ports` → `ServiceRegistry` (дополнение к docker-compose) | ADR-011, ADR-022 |
| 18 | `IsolatedParser`: `errors="replace"` при чтении, `/test/` исключение по пути, `MinimalClassNode` при сбое класса — не останавливаться на одном сломанном файле | ADR-001, ADR-019 |
| 19 | javalang fallback: вызывать когда tree-sitter даёт `error_ratio > 0.1` | ADR-001 |
| 20 | BFS fallback иерархия: exact method → class → all classes in file → service (не возвращать пустой blast radius) | ADR-003 |
| 21 | `USES_DTO` рёбра: импорты из `ts-common` / shared модулей → DTO узлы | ADR-002, ADR-003 |
| 14d | **MockMvc URL → RouteIndex `test_hint`**: `extract_test_route_hints()` на `src/test/` — `MockMvcRequestBuilders.get("/api/v1/...")` → 250 уникальных путей; `source=test_hint`; приоритет ниже `@RequestMapping`; путь только в тесте → `technical_debt[ORPHAN_TEST_URL]` | ADR-025 §A.3 |

---

## Tier 3 — Качество и полнота (сделать после первого IoU замера)

| # | Задача | ADR |
|---|---|---|
| 22 | **Классификатор строк** (4 ступени): структурная проверка → контекст вызова → regex → BGE-M3. Без этого `"email"` в sendEmail и в convertAndSend неразличимы; `"PAID"` создаст ложный CALLS_SERVICE | ADR-023 |
| 23 | **@Value жизненный цикл**: `${key:default}` parsing; lookup chain: ENV_INDEX → application.yml → docker-compose → fuzzy BGE-M3 → default; shadow `// @Value` → SHADOW_CALLS_SERVICE | ADR-023 |
| 24 | **ConceptRegistry** — language-agnostic таблица концептов (http_endpoint, async_call, db_access, config_inject, message_send, ...); диспатч на Java/Python/Go/Node.js детекторы | ADR-022 |
| 25 | **Embedding fallback** для ConceptRegistry + строковых классификаторов: BGE-M3 cosine по примерам; кеш при инициализации | ADR-022, ADR-023 |
| 26 | Конфиг-парсеры: `application.yml` / `application.properties` → `@Value` ключи в `ENV_INDEX` | ADR-011 |
| 25 | Anchor vs Active taxonomy: предупреждение при anchor-старте BFS (`SERVICE`, `DTO`, `INTERFACE`), разные base criticality | ADR-020 |
| 26 | **Inline doc extraction → `doc_summary`**: (1) Java `logger.info/warn/error("[METHOD][DESCRIPTION][param: {}]")` — структурированный формат во всех сервисах, первый bracket = метод, второй = описание → `doc_summary` узла; (2) Python/Go `print("[TAG] ...")` — та же семантика, language-agnostic через ConceptRegistry; (3) Китайские `#` комментарии — BGE-M3 нативно поддерживает, включать в `emb_semantic` as-is (не фильтровать non-ASCII); `~~text~~` = done, `~text` = in-progress, `5. text` без маркера = TODO → `TECH_DEBT[ORPHAN_TODO]`; (4) `FLOW_STEP_PATTERN` (//1. //2.) → flow-step метаданные | ADR-020, ADR-025 §F |
| 27 | Shadow connections: закомментированный `@Value("${...}")` → `SHADOW_CALLS_SERVICE` с conf=0.90; Two-Phase: найти закомм. блоки → классифицировать | ADR-016, ADR-020, ADR-022 |
| 28 | CFG для условных вызовов: `if (tripId.startsWith("G"))` → два условных `CALLS_SERVICE` с `cfg_condition` атрибутом | ADR-021 |
| 29 | Reaching definitions + alias analysis: точный URL у `restTemplate.exchange(requestUrl, ...)` через статический AST обход | ADR-021 |
| 30 | Lightweight type inference: `@Autowired OrderService` → `SymbolIndex.get_implementors()` → `OrderServiceImpl` | ADR-021 |
| 31 | Архитектурные аннотации: `@Transactional`, `@Async`, `@PostConstruct`, `@Scheduled` → ConceptRegistry (language-agnostic!) → метаданных узла, `warnings[]` в JSON | ADR-021, ADR-022 |
| 32 | Spring Events / Kafka Topics: Two-Phase → найти Config-классы / `@EnableKafka` → извлечь топики → `ASYNC_MESSAGE` | ADR-021, ADR-022 |
| 33 | Circuit breakers + `@Cacheable`: через ConceptRegistry; `@Cacheable` → `SHARES_RESOURCE` через кэш-имя | ADR-021, ADR-022 |
| 34 | Tech debt detection: dead service ref (нет в docker-compose), dangling caller (нет handler в RouteIndex), dead message (нет @RabbitListener) → `technical_debt[]` в JSON | ADR-021 |
| 34b | **readme.txt legacy nginx парсер**: `parse_readme_file()` → `proxy_pass` маршруты с confidence=0.40; code snippets с confidence=0.25; детектор мёртвых сервисов (`ts-login-service`, `ts-register-service`, `ts-sso-service`) → `technical_debt[type=dead_doc_ref]` | ADR-028 |
| 34c | **Shadow `// //` рекурсивные комментарии**: `parse_comment_node(depth)` — уровень 2 внутри блока уровня 1 = sub-alternative → пропустить; `////...` декоративные разделители → `is_decorative_divider()`; `shadow_conf(base, depth)` = `base - (depth-1) × 0.15`; ts-news-service `MONGODB_URL` → `SHADOW_SHARES_RESOURCE`; `is_vendor_doc()` только для angular.js и old-docs | ADR-025 §C |
| 35 | Bidirectional BFS O(b^(d/2)) вместо O(b^d) — заметно на больших репо | ADR-005 |
| 36 | Риск-формула и веса рёбер: `CALLS=1.0`, `SHARES_RESOURCE=0.5`, `USES_DTO=0.3`, `USES_MAGIC_CALL=1.5`, `ASYNC_CALL=0.6` | ADR-005 |
| 36b | **NodeMeta enrichment**: `pagerank` + `in_degree/out_degree` из NetworkX; `has_tests` = `find *Test.java` по сервису; `churn_score` = `git log --follow --format=%H` count/total; `doc_summary` из Javadoc первый параграф / `@ApiOperation`; fallback Ollama (Tier 4) | ADR-030 §F |
| 36c | **SCC blast clusters** (поднято из Tier 5): `nx.strongly_connected_components()` O(V+E) после `GraphBuilder.build()`; кластеры ≥ 2 узлов → при касании любого в BFS добавлять весь кластер в blast radius; `technical_debt[CIRCULAR_CLUSTER]`; решает P-94 | ADR-005 |
| 36d | **Bag of Business Tokens**: доменные токены из endpoint-путей + имён методов + имён таблиц (без стоп-слов: http, service, get, post, …) → профиль сервиса; гибридный BM25+Dense (alpha=0.5) поиск для локализации сервиса по семантическому запросу («отмена заказа»); решает P-93 | ADR-033 §C, ADR-030 |

---

## Tier 4 — Семантика и ML (если есть время)

| # | Задача | ADR |
|---|---|---|
| 37 | BGE-M3 эмбеддинги узлов: контекстный шаблон `[SERVICE:x][PKG:y][ANN:z][SIG:...]`; кеш примеров концептов при инициализации | ADR-020, ADR-022 |
| 37b | **FileSimilarityIndex BGE-M3 апгрейд**: stdlib-версия готова (`src/similarity/`, ADR-031); здесь — замена `_ast_vecs`/`_ctx_vecs` на BGE-M3 эмбеддинги (1024-dim) + LanceDB HNSW для O(log n) вместо O(n); публичный API не меняется | ADR-031, ADR-022, ADR-027, ADR-030 |
| 37c | **Три гранулярности на узел**: `emb_struct` (скелет ~150 токенов) + `emb_semantic` (роль+аннотации ~400) + `emb_full` (полный текст ~1500); LanceDB-схема с тремя колонками; BGE-M3 даёт все три в одном инференсе | ADR-030 §A |
| 37d | **BM25 гибридный поиск**: BGE-M3 sparse_vecs → `(indices, values)` в LanceDB; `hybrid_score = alpha×dense + (1−alpha)×sparse`; `_is_identifier_query()` → alpha=0.3 для camelCase/UPPER_CASE, 0.7 для семантических | ADR-030 §B–C |
| 38 | Per-role cosine thresholds: `ENV_PATTERN=0.82`, `CODE_STRUCTURE=0.88`, `SEMANTIC_SEARCH=0.65` | ADR-020 |
| 39 | Pattern store (unified rules): 5 типов (`string_match`, `regex`, `template` со слотами, `tree_sitter_query`, `vector_centroid`) + ts-query/regex валидация перед сохранением | ADR-018, ADR-014 |
| 40 | Semantic bridge: dead-end BFS → BGE-M3 cosine candidates → SEMANTIC_BRIDGE рёбра | ADR-005 |
| 41 | EnvironmentProfile Tier 1–4: адаптировать тактику под наличие docker-compose / gateway YAML; Two-Phase глубина по Tier | ADR-019, ADR-022 |
| 42 | Auto-rule Ollama loop: BGE-M3 DBSCAN кластеризация → Ollama на центроид → ts-query правило → валидация | ADR-022, ADR-014 |
| 42b | **Map-Reduce сервисных векторов**: `reduce_service_embedding()` = взвешенное mean-pooling по PageRank файлов; отдельная `service_table` в LanceDB; двухфазный поиск: сервис → файлы | ADR-030 §D |
| 42c | **PassportRegistry Level-2 fine drill**: Level-1 BFS (~2/39 кандидатов) → запустить файловые адаптеры только для кандидатов (5% загрузка вместо 100%); интеграция с GraphBuilder через callback `on_candidates(set[str])` | ADR-033 §Level-2 |
| 42d | **`--bytecode-hints <target-dir>`**: `enrich_from_bytecode()` через `jawa`; извлечь строки constant pool → compile-time склеенные URL → RouteIndex; имена очередей → верификация SymbolIndex; сервисные имена `ts-*` → подтверждение рёбер | ADR-026 |
| 37e | **`_classify_method_tier()` HIGH/MEDIUM/LOW**: `@RestController/@Service` → HIGH (все три emb); `@Repository/restTemplate` → MEDIUM (emb_struct + emb_semantic); геттеры/сеттеры/toString → LOW (только emb_struct или пропустить); снижение шума при retrieval | ADR-030 (чанкинг) |

---

## Tier 5 — Сложное / необязательное (только если всё остальное готово)

| # | Задача | Сложность | ADR |
|---|---|---|---|
| 43 | **SCC/Tarjan blast clusters** → поднята в Tier 3 как task 36c | — | ADR-005 |
| 44 | Articulation points → AUTO HIGH criticality (удаление этого узла разрывает граф) | Алгоритм | ADR-005 |
| 45 | Landmark pre-computation для hot nodes (ts-common, gateway) | Оптимизация | ADR-005 |
| 46 | tree-sitter error recovery gradient: хирургическое удаление строки → биссекция → Ollama repair (MAX=3, только local) | Отладка | ADR-021 |
| 47 | Полиглотные сервисы через ConceptRegistry: Python/Flask (`ts-avatar`), Node.js (`ts-ticket-office`), Go (`ts-news`) | Отладка | ADR-001, ADR-022 |
| 48 | Kafka топики (Two-Phase): найти `@EnableKafka`/`KafkaProperties` → извлечь топики → ASYNC_MESSAGE | Отладка | ADR-011, ADR-022 |
| 49 | PostgreSQL/pgvector хранение вместо pickle (нужен Docker) | Инфра | ADR-008 |
| 50 | React UI / sigma.js визуализация графа | Инфра | ADR-010, ADR-012 |
| 51 | Микросервисная архитектура (engine-api + worker-service + ml-service) | Инфра | ADR-009 |
| 52 | **ColBERT hot-nodes**: `should_use_colbert()` — только узлы с `pagerank > 0.01` или `in_degree > 10` (gateway, ts-common, ключевые интерфейсы); MaxSim scoring через BGE-M3 `colbert_vecs`; recall@10 ~0.95 vs ~0.87 dense; RAM: 512 векторов × 1024 × 4 bytes/node | ML | ADR-030 §E |
| 53 | **MinHash + LSH** для `FileSimilarityIndex`: при >10K файлов (>200 сервисов) попарное O(n²) становится узким местом → 128-шингловые MinHash сигнатуры + `datasketch.MinHashLSH` (Jaccard ≥ 0.5) → O(n) при запросе; upgrade path для P-95 поверх ADR-031, публичный API не меняется | Оптимизация | ADR-031, ADR-027 |

---

## Ориентиры по срокам

```
День 1–2:  Tier 1 (задачи 1–11) → первый `python -m blast_radius analyze ... | jq .`
День 3:    Tier 2 (задачи 12–21) → baseline IoU замер с жюри
День 4–5:  Tier 3 (задачи 22–36) → прирост IoU
День 6:    Tier 4 (задачи 37–42) если IoU < цели
День 7:    Tier 5 по необходимости / чистка / docs
```

---

## Выполнено (зафиксировано, не повторять)

| Задача | Результат | Файлы | ADR |
|---|---|---|---|
| **IsolatedParser + arch_pattern + node_details** (task 18 частично, ADR-004, ADR-007) | walker: `src/test/` исключён (-875 тестовых узлов, граф 2216 узлов); MinimalClassNode fallback при сбое класса; `detect_arch_pattern()`: 38 MVC / 1 EDA / 1 FLAT / 87 UNKNOWN; `node_details` в JSON output (flow_steps, arch_role, criticality, arch_pattern, logger_tags) | `src/core/walker.py`, `src/graph/builder.py`, `src/graph/node_typing.py`, `src/graph/bfs.py`, `src/output/serializer.py` | ADR-004, ADR-019 |
| **FindingIngester** (task 7, 12, 13 интеграция) | Единая точка трансляции `AnalysisResult → граф`. Матрица: CALLS_SERVICE / PROXY_ROUTE / SERVICE_DEF / HTTP_ENDPOINT / CALLS_DB / ASYNC_CALL. MIN_CONFIDENCE=0.7. 169 findings ingested, +CALLS_DB×25, +ASYNC_MESSAGE×5, +INFRA×22 | `src/graph/ingester.py` | — |
| **source_pipeline** (архитектура) | `default_registry()` фабрика: DI через registry=None в GraphBuilder. walker расширен `config_detector: Callable` параметром | `src/core/source_pipeline.py` | — |
| **SIBLING_OF рёбра** (task 7b) | `detect_siblings_by_rules()`: other(0.85), numeric(0.85), version(0.85), admin(0.60), func(0.60). 19 рёбер. BFS: depth_cost=0 | `src/graph/siblings.py`, `src/graph/bfs.py` | ADR-027 |
| **SHARES_RESOURCE рёбра** (task 7) | Сервисы разделяющие db:: узел → SHARES_RESOURCE weight=0.5. 10 пар на train-ticket | `src/graph/builder.py` (`_add_shares_resource`) | ADR-011 |
| **PackageIndex** (task 8b) | `build_from_parsed()` без двойного чтения; `classify_import()` → external/library/service. 262 пакета, 42 сервиса | `src/graph/package_index.py` | ADR-029 |
| **USES_DTO рёбра** (task 21) | `ParsedJavaFile.imports` + `_IMPORT_RE`; двухшаговый build. 37 рёбер. `_NO_BACKWARD={"USES_DTO"}` в BFS | `src/parsers/java.py`, `src/graph/builder.py`, `src/graph/bfs.py` | ADR-002 |
| **Polyglot парсеры** (task 8c) | `LanguageParser` Protocol + `PythonParser` (Flask/Tornado/FastAPI) + `GoParser` (net/http/gin/web). GraphBuilder принимает `list[LanguageParser]`. `_add_common()` для Python/Go. 262 ENDPOINT узлов, +2 CALLS_SERVICE (ts-voucher→order) | `src/parsers/base.py`, `src/parsers/python.py`, `src/parsers/go.py`, `src/graph/builder.py` | ADR-029 |
| **Баг JavaParser**: `EndpointInfo` вместо `JavaEndpointInfo`** (Task 18 частично) | `_parse_endpoint_annotations` создавал базовый `EndpointInfo` без `class_fqn`/`method_name` → tree-sitter ошибка на 40 файлах. Исправлено на `JavaEndpointInfo`. `files_error: 40→0`, `ENDPOINT: 7→262` | `src/parsers/java.py` | — |
| **BFS fallback иерархия** (task 20) | `_resolve_start` → tuple `(nodes, level)`. Иерархия: exact_method → class → file_classes (по `spec.file`) → service → not_found. `BlastRadiusResult.resolution_level` + warnings при fallback | `src/graph/bfs.py` | ADR-005 |
| **RabbitMQ ASYNC_MESSAGE** (task 16) | `convertAndSend(Queues.queueName)` → sender ребро; `@RabbitListener(queues=X)` → receiver ребро. 6 рёбер: preserve×2→email, notification→email, food→food_delivery, food_delivery→delivery, email→notification. BFS обходит ASYNC_MESSAGE в обе стороны | `src/parsers/java.py`, `src/graph/builder.py`, `src/graph/bfs.py` | ADR-003 |
| **SymbolIndex** (task 10) | `build_from_parsed()` из `ClassInfo.static_constants`. `resolve("Queues.queueName", service)` → `"email"`. 17 FQN / 13 simple-key записей. Предпочтение своему сервису при коллизии. Баг: `${rabbitmq_host:localhost}` → `_resolve_spring_default()` в ingester | `src/graph/symbol_index.py`, `src/graph/ingester.py` | ADR-023 |
| **Типизация узлов** (ADR-002, ADR-004, ADR-020) | `resolve_node_type()`: CLASS/INTERFACE/REPOSITORY/ABSTRACT_CLASS по kind+annotations+extends. `arch_role_from_package()`: пакет → http_handler/business_logic/data_access/…. `node_attrs()`: каждый узел получает `bfs_role` (anchor/active) + `criticality`. `LIBRARY` для ts-common. Итог: 67 INTERFACE + 27 REPOSITORY + 1 LIBRARY из 3091 узлов | `src/graph/node_typing.py`, `src/graph/builder.py`, `src/graph/ingester.py`, `src/parsers/java.py` | ADR-002, ADR-004, ADR-020 |
| **SimilarityIndex stdlib-версия** (часть 37b) | 5 уровней: hash / body_exact / ast_exact / ast_vector / ctx_vector. 479 файлов за 0.27с | `src/similarity/index.py` | ADR-031 |
| **n-грамм вектор в `_hint()`** (DepsAdapter) | 3-уровневый каскад: exact → substring → cosine(trigrams, θ=0.40). 15/15 тестов | `src/adapters/tools/deps.py` | ADR-032 |
| **Баг DockerAdapter**: `name='java'` | `_name_from_image()` предпочитает директорию над именем образа | `src/adapters/tools/docker.py` | — |
| **Баг DepsAdapter**: `package=':'` из root pom.xml | `continue` при пустых groupId + artifactId | `src/adapters/tools/deps.py` | — |
| **Баг DepsAdapter**: Flask/gunicorn без `tech_hint` | Добавлены `web:flask`, `web:gunicorn`, `web:fastapi`, `web:django`, `web:express` | `src/adapters/tools/deps.py` | ADR-032 |
| **Баг YamlAdapter**: multi-document YAML | `yaml.safe_load_all()` + `_process_doc()` — `svc.yaml` даёт 88 findings вместо ошибки | `src/adapters/tools/yaml_adapter.py` | — |
| **Баг YamlAdapter**: `host='${TRAVEL_MYSQL_HOST:…}'` | `_resolve_spring_defaults()` перед `parse_url()` | `src/adapters/tools/yaml_adapter.py` | — |
| **Баг YamlAdapter**: CALLS_SERVICE из `*.url` | `_scan_spring_service_urls()` + `_flatten_service_urls()` → 5 CALLS_SERVICE | `src/adapters/tools/yaml_adapter.py` | — |
| **Баг NginxAdapter**: location `~*` | `args[0] in _LOCATION_MODIFIERS` → `location_path = args[1]` | `src/adapters/tools/nginx.py` | — |
| **Баг NginxAdapter**: `include mime.types` в errors | `_INCLUDE_NOISE` regex → `metadata["suppressed_include_errors"]` | `src/adapters/tools/nginx.py` | — |
| **TestAdapter** (Two-Phase, Java тесты) | 79 тест-файлов, 92 CALLS_SERVICE + 251 HTTP_ENDPOINT, 35 caller→callee пар | `src/adapters/tools/test_adapter.py` | ADR-025 §E |
| **`ParsedURL.is_bind_all`** | Property для `0.0.0.0`/`::` | `src/adapters/tools/base.py` | — |
| **ServicePassport + PassportRegistry** | MAP→REDUCE агрегация: Level-1 BFS по ~68 паспортам (0.08с) | `src/graph/passport.py` | ADR-033 |
| **Баг DepsAdapter**: pom.xml 0 findings | XML Element falsy — исправлено `if child is None` | `src/adapters/tools/deps.py` | — |
| **Файловый обходчик** (task 1) | EXCLUDED_DIRS, language detection, `service_name_of()` | `src/core/walker.py` | ADR-001 |
| **NodeId формат** (task 4) | `{service}::{fqn}` / `{service}::{fqn}.{method}` | `src/core/node_id.py` | ADR-002 |
| **Java парсер** (task 2) | tree-sitter структура + regex вызовы. 479 файлов, 0 ошибок | `src/parsers/java.py` | ADR-001 |
| **NetworkX граф** (task 3) | SERVICE/CLASS/METHOD/ENDPOINT узлы, все типы рёбер. 2575 узлов, 2618 рёбер | `src/graph/builder.py` | ADR-002 |
| **BFS blast radius** (task 5) | backward CALLS_SERVICE + forward CONTAINS. ts-train-service → 13 сервисов, 835 узлов | `src/graph/bfs.py` | ADR-005 |
| **JSON вывод** (task 6) | `python -m src analyze <repo> --service ts-x` | `src/output/serializer.py`, `src/__main__.py` | ADR-007 |

---

## Что можно отключить без поломки

| Компонент | Флаг |
|---|---|
| Семантика / эмбеддинги | `--no-semantic` |
| Ollama LLM | `--no-llm` |
| Shadow connections | `--no-shadow` |
| Полиглот | `--java-only` |
| Risk score в выводе | `--minimal-output` |
| Глубина обхода | `--max-depth N` |
