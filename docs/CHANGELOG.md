# Changelog

## [Unreleased]

### Добавлено (двадцать третий блок — PackageIndex, полиглот URL, интерфейсы)
- `docs/adr/ADR-029-package-index-polyglot-interfaces.md` — новый ADR: (A) `build_package_index()` — программный сбор пакетного префикса → сервис через скан `src/main/java/`; полная таблица 41 пакет; коллизия `fdse.microservice.*` — ts-basic-service и ts-station-service используют одинаковый пакет; disambiguate по субпакету через `build_fdse_disambiguate()`; `edu.fudan.common` → тип LIBRARY (не SERVICE, нет в docker-compose services:); `classify_import()` — three-way разбиение external/library/service; (B) Python `os.getenv()` URL-паттерн: `order_url = 'http://ts-order-service:12031'` + override из env; верификация: docker-compose quickstart не выставляет `ORDER_SERVICE_URL` → дефолт активен; `extract_python_service_urls()` с compose-env-lookиp; Node.js ts-ticket-office-service — только MySQL env (не CALLS_SERVICE); Go ts-news-service — весь HTTP-код закомментирован, статический ответ; (C) Spring Data: 27 репозиториев с `extends CrudRepository/JpaRepository/MongoRepository` — нет Impl-файла; `classify_java_type()` → REPOSITORY-тип → не искать реализацию; 86 интерфейсов, 121 класс с implements; `IMPLEMENTS` ребро class→interface; (D) кросс-сервисных импортов между сервисами нет — только через edu.fudan.common; `import_to_edges()` строит USES_DTO рёбра
- `docs/README.md` — добавлен ADR-029

### Добавлено (двадцать второй блок — readme.txt legacy nginx, мёртвые сервисы)
- `docs/adr/ADR-028-ui-static-ajax-routes.md` — добавлен раздел F: `ts-ui-dashboard/readme.txt` — фрагмент старого nginx.conf; маппинг legacy-путей `/cancelOrder`, `/station/*`, `/travel/*` на сервисы без `/api/v1/`; объяснение архитектурной эволюции (прямые вызовы → gateway); `ts-login-service`, `ts-register-service`, `ts-sso-travel.service` — мёртвые сервисы (не существуют в репо); `parse_readme_file()` + `detect_dead_doc_refs()`; обновлены приоритеты RouteIndex: 7 уровней (readme.txt legacy conf=0.40, code snippets conf=0.25); `DEAD_SERVICES_FROM_DOCS` — словарь для техдолга
- `docs/DEVPLAN.md` — добавлена задача #34b (readme.txt legacy nginx парсер, Tier 3)

### Добавлено (двадцать первый блок — UI статика AJAX маршруты)
- `docs/adr/ADR-028-ui-static-ajax-routes.md` — новый ADR: (A) два типа JS: Angular assets/js/ → /api/v1/ через nginx → gateway (авторитетный источник); jQuery js/ → /cancelOrder /food/getFood без /api/v1/ (legacy или Node.js; confidence=0.5); nginx.conf: единственный proxy_pass location /api/v1/ → ts-gateway-service:18888; (B) 49 уникальных /api/v1/ URL из 15 Angular-файлов → 27 сервисов вызываются напрямую из UI; 14 backend-internal сервисов не вызываются из UI (ts-seat-service, ts-price-service, ts-notification-service и др.); (C) `extract_ui_routes()`: ANGULAR_URL_CONCAT_RE + динамические конкатенации + infer_http_method(); (D) приоритет RouteIndex: gateway.yml > @RequestMapping > UI Angular (conf=0.95) > MockMvc hint (0.80) > legacy jQuery (0.50); (E) ts-ui-dashboard как SERVICE-узел с 27 исходящими CALLS_SERVICE рёбрами; ts-avatar-service обнаружен только через UI-парсер
- `docs/PROBLEMS.md` — добавлена секция 13 (P-79..P-81): CALLS_SERVICE из UI, legacy jQuery пути, ts-avatar-service через upload.js; счётчик обновлён до 81
- `docs/README.md` — добавлен ADR-028 в mermaid-карту и таблицу

### Добавлено (двадцатый блок — сервисы-родственники SIBLING_OF, семантика файлов)
- `docs/adr/ADR-027-service-siblings-semantic-files.md` — новый ADR: (A) 8 sibling-групп в train-ticket — order/order-other, preserve/preserve-other, travel/travel2, food/station-food/train-food/food-map, consign/consign-price, admin-*, route/route-plan, delivery/food-delivery; CFG-условие разделения `trainNumber.startsWith("G") || trainNumber.startsWith("D")` верифицировано в SeatServiceImpl; (B) 3-фазный детектор: rule-based (8 паттернов) → structural Jaccard (нормализованные имена классов) → BGE-M3 (< 47² пар); для train-ticket rule-based покрывает 100%; (C) `SIBLING_OF` ребро с `split_condition`, `split_type`, `conf`; TRAVERSAL_POLICY: depth_cost=0, weight=0.85/0.6/0.5 по типу; (D) `FileSimilarityIndex`: нормализация имён классов (`normalize_class_name` убирает domain-prefix), кросс-сервисные эмбеддинги top-K (min_sim=0.85), расширение blast radius для sibling-файлов
- `docs/DEVPLAN.md` — добавлены задачи #7b (SIBLING_OF rule-based, Tier 1) и #37b (FileSimilarityIndex, Tier 4)
- `docs/PROBLEMS.md` — добавлена секция 12 (P-75..P-78): зеркальные сервисы, split_condition, нормализация классов, кросс-сервисные эмбеддинги; счётчик обновлён до 78
- `docs/README.md` — добавлен ADR-027 в mermaid-карту и таблицу

### Добавлено (девятнадцатый блок — байт-код анализ .jar)
- `docs/adr/ADR-026-bytecode-analysis.md` — новый ADR: (A) в репо только `maven-wrapper.jar` × 5, идентичны (MD5 совпадают) — инструментальный файл Maven, не код сервисов; (B) `jawa` 2.2.0 (pure Python, без JVM) — парсит .class файлы, извлекает FQN, методы, строки из constant pool; (C) главное преимущество bytecode: compile-time constant folding → готовые склеенные URL без Value Flow Graph; (D) в train-ticket не актуально (URL строятся через runtime getServiceUrl()), поэтому Tier 4; флаг `--bytecode-hints <target-dir>`; `enrich_from_bytecode()` как опциональное обогащение RouteIndex и верификации SymbolIndex
- `docs/README.md` — добавлен ADR-026 в mermaid-карту и таблицу

### Добавлено (восемнадцатый блок — тестовые файлы, моки, рекурсивные комментарии)
- `docs/adr/ADR-025-test-files-mock-shadow-comments.md` — новый ADR: (A) политика тестовых файлов: `src/test/` исключается из графа; 92 `@Mock RestTemplate` не создают CALLS_SERVICE; 250 MockMvc URL-путей → `test_hint` для RouteIndex (подтверждение, не первичный источник); приоритет: gateway.yml > @RequestMapping > test_hint; единственный `@SpringBootTest` — пустой smoke test; в main нет ни одного *Mock.java; (B) `old-docs/` полностью исключается (1018 строк `//// ` засорили бы shadow граф); (C) рекурсивные комментарии: в активных сервисах train-ticket `// //` = 0 совпадений; алгоритм `parse_comment_node()` рекурсивно снимает `//` prefix; depth-penalty: `conf - (depth-1) × 0.15` (уровень 1=0.90, уровень 2=0.75, уровень 3=0.60 → ниже порога → пропуск); JS-специфичный паттерн в travel.js — уровень 2 = замена синтаксиса, игнорировать; (D) таблица решений по типам файлов
- `docs/PROBLEMS.md` — добавлена секция 11 (P-69..P-73): моки в тестах, MockMvc URL hints, рекурсивные комментарии, закомментированные методы, old-docs исключение; счётчик обновлён до 73 проблем
- `docs/README.md` — добавлен ADR-025 в mermaid-карту и таблицу; счётчик проблем обновлён

### Добавлено (семнадцатый блок — FQN SymbolIndex, bi-encoder реранкер, аномалии train-ticket)
- `docs/adr/ADR-024-symbol-index-fqn-reranker.md` — новый ADR: (A) критическая ошибка non-FQN SymbolIndex — 5 классов `Queues.java` в разных сервисах перезаписывают друг друга; `StaticFieldEntry` с FQN (package+class+field); `SymbolIndex.resolve_ref()` через imports → wildcard → same-package → bi-encoder; `index_static_fields()` собирает пакет+класс+поле из tree-sitter AST; (B) regex + bi-encoder реранкер для неоднозначных ссылок: контекст использования vs контексты объявления, same-service bonus +0.2, порог 0.65; (C) найденные аномалии: CIRCULAR на очереди "email" в ts-notification-service (шлёт и слушает сам себя); hardcoded IP `10.176.122.1` как default в datasource 5 сервисов; @Value без дефолта и без compose (`${test_send_mail_user}`, `${swagger.controllerPackage}`); ts-consign-mysql только в quickstart compose, не в основном; (D) обновлённый 8-шаговый @Value lookup chain с шагом для quickstart-docker-compose и нормализацией ключей; (E) итоговая таблица 6 исправлений для реализации
- `docs/PROBLEMS.md` — добавлена секция 10 (P-63…P-68): FQN коллизия SymbolIndex, wildcard import + bi-encoder, CIRCULAR RabbitMQ, hardcoded IP, ts-consign-mysql только в quickstart, @Value без определения; счётчик обновлён до 68 проблем
- `docs/README.md` — добавлен ADR-024 в mermaid-карту и таблицу; PROBLEMS.md обновлён до 68 проблем
- `docs/DEVPLAN.md` — задача #10 уточнена: FQN static field resolver с import-резолвингом и bi-encoder реранкером; ссылка на ADR-024

### Добавлено (шестнадцатый блок — жизненный цикл строк и конфигов, классификатор)
- `docs/adr/ADR-023-string-value-lifecycle.md` — новый ADR: (A) жизненный цикл переменной-конфига — 6 фаз источника (S1-S6: литерал, @Value, getServiceUrl, inline, статическая константа, System.getenv) + 5 фаз передачи (T1-T5: присваивание, условное, конкатенация, параметр, поле) + 3 фазы затирания (O1-O3); Value Flow Graph без выполнения кода; (B) классификатор строк 4 ступени: структурная проверка O(1) → контекст вызова → regex → BGE-M3 векторный; ключевое: `"email"` в `convertAndSend` = QUEUE_NAME, `"email"` в `sendEmail` = DATA; `"PAID"` = BUSINESS_STATUS → ложных CALLS_SERVICE нет; (C) resolve_static_field_ref: `Queues.queueName` → `"email"` через SymbolIndex (до этого имена очередей не разрезолвлялись); (D) expand_value: полное разворачивание `uri = url + "/path"` → `exchange(uri + "/" + id)` до конкретного URL включая условные ветки; (E) @Value lookup chain: `${key:default}` → ENV_INDEX → application.yml → docker-compose → fuzzy BGE-M3 → default; (F) таблица что НЕ создаёт ребро: BUSINESS_STATUS / TEMPLATE_NAME / SQL / UUID / logger.info / тестовые файлы
- `docs/DEVPLAN.md` — задачи 22-23 заменены на классификатор строк и @Value lifecycle; 25 = Embedding fallback для обоих ADR-022 и ADR-023; нумерация сдвинута
- `docs/README.md` — добавлен ADR-023

### Добавлено (пятнадцатый блок — language-agnostic паттерны, Two-Phase, пропущенные конфиги)
- `docs/adr/ADR-022-language-agnostic-patterns.md` — новый ADR: (A) Two-Phase Discovery тактика "не в лоб" — сначала локализовать кандидатов (файлы/классы), потом извлекать детали из них; применимо к Kafka, datasource, nginx, любым конфигам в коде; (B) ConceptRegistry — language-agnostic таблица 11 концептов (http_endpoint, async_call, db_access, config_inject, message_send, ...) с детекторами для Java/Python/Go/Node.js; диспатч по языку файла; (C) Embeddings as Fallback — если regex нашёл 0 совпадений или язык неизвестен → BGE-M3 cosine по примерам концепта; `ConceptEmbeddingCache` кеширует при инициализации; (D) pipeline regex → embedding fallback для всех паттернов ADR-021; (E) Two-Phase для Config-классов (`*Config`, `@Configuration`, `@EnableKafka`); (F) обновлённые Tier 1–4 для Two-Phase
- `docs/adr/ADR-011-config-file-parsers.md` — добавлены: nginx.conf как 🔴 Критично с `proxy_pass` → CALLS_SERVICE; Kubernetes `svc.yaml` повышен до 🟠 Высокий; `spring.application.name` → авторитетное имя сервиса; `spring.datasource.url` → SHARES_RESOURCE по DB-хосту; Nacos → `uses_nacos` атрибут; Kafka паттерн для других репо; раздел nginx парсера с примером кода
- `docs/DEVPLAN.md` — задачи перенумерованы (51 задача); добавлены: #9 Two-Phase базовый каркас, #10 nginx парсер, #11 application.yml расширенный, #17 K8s svc.yaml, #22 ConceptRegistry, #23 Embedding fallback, #48 Kafka Two-Phase; Tier 3 аннотации теперь помечены как language-agnostic через ConceptRegistry
- `docs/README.md` — добавлен ADR-022

### Добавлено (четырнадцатый блок — тактический план разработки)
- `docs/DEVPLAN.md` — тактический чеклист: 45 задач по 5 уровням приоритета (Tier 1 = MVP обязательно → Tier 5 = сложное/необязательное); каждая задача привязана к ADR; ориентиры по дням (Tier 1–2 = дни 1–3, Tier 3 = дни 4–5); таблица отключаемых компонентов через флаги
- `docs/README.md` — DEVPLAN.md добавлен как первая строка секции планирования

### Добавлено (тринадцатый блок — compiler-inspired, техдолг, error recovery)
- `docs/adr/ADR-021-compiler-techniques-debt-recovery.md` — новый ADR: (A) Compiler-inspired статический анализ без выполнения: CFG для условных CALLS_SERVICE, reaching definitions для точных URL-значений, alias analysis, type inference через SymbolIndex.get_implementors(); (B) архитектурные аннотации не в JSON: @Transactional(tx_propagation), @Async(executor), @PostConstruct, Spring Events→ASYNC_MESSAGE, @Scheduled, circuit breakers, @Cacheable→SHARES_RESOURCE, @ControllerAdvice; (C) детекция 5 видов техдолга: dangling caller, orphan endpoint, dead message, dead service ref (подтверждён: rest-service-external:16100 в AsyncTask), missing @Value; (D) градиент восстановления tree-sitter: ratio < 1% = clean; 1–10% = хирургическое удаление строки; 10–30% = бинарная биссекция; ≥30% = javalang fallback; крайний случай = Ollama qwen2.5-coder repair (MAX=3, только local); `parse_confidence` → criticality weight
- `docs/PROBLEMS.md` — добавлена секция 9 (P-54…P-62): CFG conditional edges, reaching defs, alias analysis, type inference, @Transactional blast isolation, архитектурные аннотации, dead service ref, односторонние связи, error recovery gradient
- `docs/README.md` — добавлен ADR-021 в mermaid-карту и таблицу; PROBLEMS.md обновлён до 62 проблем

### Добавлено (двенадцатый блок — таксономия узлов, паттерны, контекст)
- `docs/adr/ADR-020-node-taxonomy-embedding-context.md` — новый ADR: Anchor vs Active узлы (SERVICE/DTO=anchor, METHOD/CLASS=active) с разными base criticality; шаблонные паттерны со слотами (`template` type + `template_slots` JSONB); per-role cosine thresholds (ENV_PATTERN=0.82, CODE_STRUCTURE=0.88, SEMANTIC_SEARCH=0.65); контекстный шаблон эмбеддинга (SERVICE+ANN+FLOW_STEPS+LOGGER_TAGS+CALLS); 7 классов комментариев из реального train-ticket (flow steps, logger tags, shadow @Value, URL hints, TODO async, incomplete deps, технические маркеры); `EXCLUDED_DIRS` для target/node_modules
- `docs/PROBLEMS.md` — добавлена секция 8 (P-43…P-53): проблемы эмбеддингов, паттернов и извлечения информации
- `docs/README.md` — добавлен ADR-020 в mermaid-карту и таблицу

### Добавлено (одиннадцатый блок — единый справочник проблем)
- `docs/PROBLEMS.md` — 42 проблемы по 7 категориям (парсинг/граф/конфиги/ML/инфра/shadow/оценка); для каждой: 1-2 строки проблемы + 1-2 строки решения + ссылка на ADR; топ-10 по влиянию на IoU в конце
- `docs/README.md` — добавлена секция "Быстрый старт для анализа" с PROBLEMS.md

### Добавлено/изменено (десятый блок — мульти-сигнальный анализ)
- `docs/adr/ADR-019-multi-signal-analysis.md` — новый ADR: EnvironmentProfile (Tier 1–4), таблицы тактик для Q1–Q4, `fuse_signals()` с противоречиями, `IsolatedParser` (fail-safe на уровне файл/класс/метод), `CommentMetadata` (deprecated/bug regex), `bootstrap_unknown_service()` векторная разведка
- `docs/adr/ADR-001-parser-strategy.md` — добавлен раздел изоляции ошибок: `errors="replace"`, `/test/` exclusion по пути, `MinimalClassNode` при сбое класса
- `docs/adr/ADR-002-graph-schema.md` — добавлены поля `deprecated`, `deprecated_since`, `has_bug_note`, `bug_description` в атрибуты узла
- `docs/adr/ADR-004-node-abstraction-levels.md` — добавлен раздел влияния comment metadata на criticality: deprecated × 0.5, has_bug_note × 1.3
- `docs/adr/ADR-011-config-file-parsers.md` — добавлена таблица Tier-управления конфиг-парсерами через EnvironmentProfile
- `docs/README.md` — добавлен ADR-019 в mermaid-карту и таблицу

### Добавлено/изменено (девятый блок — анализ рисков реализации)
- `docs/adr/ADR-005-traversal-strategy.md` — заменена секция "Направление обхода" на `TRAVERSAL_POLICY` таблицу: `USES_DTO` только вперёд (backward=False) — критично для предотвращения взрыва через ts-common; `USES_MAGIC_CALL` и `SHADOW_*` — только вперёд; функция `get_neighbors()` с policy-based обходом
- `docs/adr/ADR-002-graph-schema.md` — добавлен раздел "Критический риск: совместимость NodeId с форматом оценки": aliases на узлах, нормализатор `normalize_node_id()`, конфигурируемый `NODE_ID_FORMAT`
- `docs/adr/ADR-003-fallback-strategy.md` — D.2 `getServiceUrl()` расширен: двухпроходной анализ (проход 1 — сбор хелперов по всему репо, проход 2 — инлайнинг); функции `first_pass_collect_helpers()` и `resolve_method_call()`
- `docs/adr/ADR-014-ml-pipeline.md` — добавлена обязательная валидация синтаксиса tree-sitter query и regex перед сохранением правила в БД (`validate_ts_query()`, `save_rule_validated()`); предотвращает засорение графа невалидными Ollama-правилами

### Добавлено/изменено (восьмой блок)
- `docs/adr/ADR-005-traversal-strategy.md` — параллелизм BFS: GIL-ограничение, fork CoW, параллельный BFS от разных стартов через ProcessPoolExecutor, матричный BFS (scipy sparse + BLAS), landmark pre-computation, сервисное шардирование
- `docs/adr/ADR-007-api-layer.md` — добавлен `POST /analyze/batch` (параллельный расчёт N change_spec через asyncio.gather + ProcessPoolExecutor)

### Добавлено/изменено (седьмой блок)
- `docs/adr/ADR-005-traversal-strategy.md` — алгоритмические улучшения: Bidirectional BFS O(b^(d/2)); SCC/Tarjan для blast clusters; DSU для SHARES_RESOURCE кластеров; точки сочленения как автоматическая HIGH критичность; LCA для множественных изменений; structural_hash (Merkle) для поиска клонов
- `docs/adr/ADR-018-unified-rule-store.md` — добавлен Aho-Corasick для точных строк в pre-filter (дополняет ripgrep)

### Добавлено (шестой блок)
- `docs/adr/ADR-018-unified-rule-store.md` — единая таблица rules (все YAML-seed + framework profiles + ROLE_MAP + generated_rules); ripgrep как pre-filter перед tree-sitter (~3x ускорение); rule-triggered reindex через rules_version

### Изменено (шестой блок)
- `docs/adr/ADR-005-traversal-strategy.md` — добавлена единая формула risk_score (edge_weight × shadow × confidence × criticality); семантический мост BFS (dead-end → semantic candidates); ранжирование blast radius при взрыве ts-common
- `docs/adr/ADR-008-storage-architecture.md` — инкрементальная индексация расширена rule-triggered reindex (rules_version в indexed_files)
- `docs/adr/ADR-011-config-file-parsers.md` — ENV_INDEX fuzzy lookup через BGE-M3; RouteIndex с glob и path-variable matching
- `docs/adr/ADR-004-node-abstraction-levels.md` — ROLE_MAP помечен как bootstrap-источник для rules table, runtime идёт через RuleEngine с fuzzy

### Добавлено (пятый блок)
- `docs/adr/ADR-017-framework-profiles.md` — YAML-профили фреймворков (Spring Boot, Flask, Tornado, Go); таксономия 5 ролей эмбеддингов (ENV_PATTERN/SHADOW_CODE/CODE_STRUCTURE/UNKNOWN_PATTERN/SEMANTIC_SEARCH); auto-rule loop: BGE-M3 кластеризация → Ollama на центроид кластера → MAX_OLLAMA_CALLS=25

### Изменено (пятый блок)
- `docs/adr/ADR-003-fallback-strategy.md` — добавлены части D и E: `${VAR:default}` уровень 0; `getServiceUrl()` инлайнинг (критично — 279 RestTemplate вызовов); `PollThread.run()` сканирование; `CommandLineRunner` как anti-edge; gateway YAML как первичный источник RouteIndex; `ASYNC_MESSAGE` рёбра от RabbitMQ
- `docs/adr/ADR-002-graph-schema.md` — добавлен тип `ASYNC_MESSAGE` (вес 0.8, metadata.queue_name)
- `docs/adr/ADR-014-ml-pipeline.md` — добавлены компоненты D и E: таблица ролей эмбеддингов, auto-rule clustering loop

### Добавлено (четвёртый блок)
- `docs/adr/ADR-016-shadow-connections.md` — теневые связи: tree-sitter детекция закомментированного кода, BGE-M3 классификатор (код vs текст), SHADOW_ типы рёбер, вес 0.4, пунктирная визуализация в UI

### Изменено (четвёртый блок)
- `docs/adr/ADR-001-parser-strategy.md` — добавлена таблица polyglot-сервисов (ts-avatar-service/Flask, ts-voucher-service/Tornado, ts-news-service/Go); shadow pipeline после основного прохода
- `docs/adr/ADR-002-graph-schema.md` — добавлены SHADOW_CALLS, SHADOW_CALLS_SERVICE, SHADOW_USES_DTO, SHADOW_SHARES_RESOURCE типы рёбер; поля shadow/shadow_confidence/comment_location в метаданных рёбер
- `docs/adr/ADR-011-config-file-parsers.md` — Python urllib паттерн (ts-voucher-service), FreeMarker .ftl как arch_role=notification, SecurityConfig → RouteIndex обогащение ролями доступа



### Изменено
- `docs/PLAN.md` — обновлён с учётом Hexagonal архитектуры, DDD, async workers; целевая структура директорий; привязка шагов к модулям
- `docs/adr/ADR-006-engine-architecture.md` — добавлена уточнённая структура проекта (`services/`), ссылки на ADR-007–012, монолитный режим
- `docs/adr/ADR-003-fallback-strategy.md` — добавлена часть D: резолвинг через URL-маршруты и RouteIndex
- `docs/adr/ADR-009-microservices.md` — исправление: worker-service имеет доступ к БД; ml-service stateless (только инференс, никакой БД)

### Добавлено (второй блок)
- `docs/README.md` — реестр документов: карта mermaid, таблица ADR, быстрый старт, инструкция добавления
- `docs/HOW_IT_WORKS.md` — полное описание двух потоков (индексация + blast radius) с sequence-диаграммами, flowchart'ами, ER-диаграммой, сквозным примером
- `docs/ARCHITECTURE.md` — статичная схема: сервисная топология, слои Hexagonal, структура репозитория, потоки данных
- `docs/adr/ADR-013-documentation-standards.md` — стандарты: Google-style docstrings, политика комментариев, README в каждом модуле, правила mermaid, реестр
- `docs/adr/ADR-015-shared-library.md` — blast-radius-core: общий домен для engine-api и worker-service, develop install

### Изменено (третий блок)
- `docs/adr/ADR-003-fallback-strategy.md` — добавлено явное разграничение: USES_MAGIC_CALL для кода, пропуск для классификационных неудач
- `docs/adr/ADR-005-traversal-strategy.md` — добавлена стратегия NetworkX pickle snapshot: BFS в памяти, lazy-loading по уровням абстракции
- `docs/adr/ADR-008-storage-architecture.md` — LanceDB удалён; добавлены: file hash инкрементальная индексация, порядок старта сервисов, shared volume для pickle
- `docs/adr/ADR-014-ml-pipeline.md` — python_predicate исключён (нет eval); иероглифы заменены; уточнение: regex только для поиска, не классификации
- `docs/adr/ADR-014-ml-pipeline.md` — Embedding-классификатор (BGE-M3, few-shot cosine), комбинированный вход (value type + name suffix), авто-бутстреп без ручной разметки, async очередь неопределённостей, Ollama Rule Engine (qwen2.5-coder 7b/14b), логарифмическая выборка, ReAct 2-3 попытки → пропустить
- `docs/adr/ADR-011-config-file-parsers.md` — матрица приоритетов файлов (🔴/🟠/🟡/🟢), Dockerfile ENV парсинг, кросс-референс между файлами

### Добавлено
- `docs/adr/ADR-007-api-layer.md` — FastAPI, разделение роутеров, Pydantic v2 DTO, CLI, lifespan
- `docs/adr/ADR-008-storage-architecture.md` — PostgreSQL/pgvector, Redis/arq, абстрактные интерфейсы, единый connections.py, wrapper/DI
- `docs/adr/ADR-009-microservices.md` — декомпозиция на 4 сервиса, Docker Compose, REST→gRPC план, параллельность запросов
- `docs/adr/ADR-010-dev-infrastructure.md` — Makefile цели, React UI (sigma.js), nginx, multi-stage Docker
- `docs/adr/ADR-011-config-file-parsers.md` — docker-compose, Helm, shell, .env, application.yml; ENV_INDEX; URL-маршрутный резолвинг
- `docs/adr/ADR-012-graph-naming-visualization.md` — compact display_name, LOD, цветовая схема, интерактивность, sigma.js
- `docs/adr/ADR-006-engine-architecture.md` — Hexagonal + DDD + async workers: структура src/, DDD-словарь, модель параллелизма
- `docs/adr/ADR-001-parser-strategy.md` — стратегия парсинга: tree-sitter + javalang fallback
- `docs/adr/ADR-002-graph-schema.md` — схема графа: типы узлов, рёбер, формат ID
- `docs/adr/ADR-003-fallback-strategy.md` — цепочки fallback для межсервисных и внутрисервисных вызовов
- `docs/adr/ADR-004-node-abstraction-levels.md` — уровни абстракции, детекция архитектурного паттерна
- `docs/adr/ADR-005-traversal-strategy.md` — алгоритм обхода графа, параметры, обоснование глубины
- `docs/PLAN.md` — поэтапный план реализации (4 фазы, зависимости, точки измерения IoU)
- `Makefile` — цели `install`, `format`, `lint`, `check`, `fix`, `install-hooks`
- `pyproject.toml` — конфигурация black / isort / mypy
- `requirements-dev.txt` — dev-зависимости (black, isort, mypy)
- `hooks/pre-commit` — git pre-commit хук с авто-форматированием
