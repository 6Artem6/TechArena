# Проблемы и решения — Blast Radius Engine

Краткий справочник. Каждая строка: проблема → идея решения → ссылка на полный ADR.

---

## 1. Парсинг и разрешение символов

| # | Уровень | Проблема | Решение | ADR |
|---|---|---|---|---|
| P-01 |  🟠 HIGH | `getServiceUrl("ts-x")` объявлен в базовом классе другого файла — tree-sitter не видит объявление | SymbolIndex: проход 0 собирает все объявления по всему репо до парсинга вызовов; рекурсивный поиск по суперклассам cap=5 | [ADR-003 §D.2](adr/ADR-003-fallback-strategy.md) |
| P-02 |  🟢 LOW | `@RequestMapping(value={"/a","/b"}, method={POST,PUT})` — массивы в аннотациях | javalang как fallback при сложных generics/массивах; явная обработка `array_initializer` | [ADR-001](adr/ADR-001-parser-strategy.md) |
| P-03 |  🟡 MEDIUM | Lombok `@Data`/`@Builder` генерирует методы не в source — `getStatus()` не найти | При `@Data` на классе синтетически добавлять геттеры/сеттеры в SymbolIndex по полям | [ADR-003](adr/ADR-003-fallback-strategy.md) |
| P-04 |  🟡 MEDIUM | `@FeignClient(name="ts-x")` — отдельный HTTP-клиентский паттерн, не RestTemplate | Явный паттерн в framework profiles; `conf=0.99`, ребро `CALLS_SERVICE` | [ADR-017](adr/ADR-017-framework-profiles.md) · [ADR-003](adr/ADR-003-fallback-strategy.md) |
| P-05 |  🟡 MEDIUM | Тестовые файлы `src/test/` дают ложные рёбра от mock-вызовов | Два прохода: **проход 0** — `src/test/` полностью пропустить (walker), в граф не добавлять; **проход 2 (подтверждение)** — после сборки основного графа читать тесты только для подтверждения уже найденных сервисных имён и путей (source=test_hint); связано с P-69 | [ADR-019 §D](adr/ADR-019-multi-signal-analysis.md) · [ADR-001](adr/ADR-001-parser-strategy.md) |
| P-06 |  🟡 MEDIUM | `ts-common` содержит 52 shared DTO — если проиндексирован после сервисов, ссылки не разрешаются | ts-common индексируется первым; порядок — из `pom.xml` (он первый как зависимость всех) | [ADR-003](adr/ADR-003-fallback-strategy.md) |
| P-07 |  🟢 LOW | Пакеты непоследовательны: `fdse.*`, `edu.fudan.*`, `order.*` — нельзя угадать по директории | FQN всегда из `package`-декларации файла, не из структуры директорий | [ADR-002](adr/ADR-002-graph-schema.md) |
| P-97 |  🟡 MEDIUM | `pom.xml` зависимости парсятся DepsAdapter → IMPORT findings, но до графа не доходят: (1) `org.services:ts-common` не получает `tech_hint` и нет `USES_LIBRARY` ребра; (2) `spring-boot-starter-amqp` не проставляет `uses_rabbitmq=True` на SERVICE; FindingIngester не обрабатывает IMPORT findings | Два шага: (A) в `_TECH_HINTS` добавить паттерн `org.services` → `library:internal`; `ts-*` artifactId → `library:ts`; (B) в FindingIngester: IMPORT с `tech_hint=mq:rabbitmq` → атрибут `uses_rabbitmq=True`; IMPORT с `groupId=org.services` → `USES_LIBRARY` ребро к LIBRARY-узлу (подтверждение PackageIndex) | [ADR-003](adr/ADR-003-fallback-strategy.md) · [ADR-029](adr/ADR-029-polyglot.md) |
| P-08 |  🟠 HIGH | Ошибка в одном файле .java ломает весь сервис | `IsolatedParser`: try/except на каждом уровне (файл → класс → метод); `PARTIAL_PARSED` узел гарантирует минимум | [ADR-019 §D](adr/ADR-019-multi-signal-analysis.md) · [ADR-001](adr/ADR-001-parser-strategy.md) |
| P-09 |  🟡 MEDIUM | Китайские символы в комментариях → `UnicodeDecodeError` | `open(errors="replace")` — всегда, без исключений | [ADR-019 §D](adr/ADR-019-multi-signal-analysis.md) |

---

## 2. Граф и обход

| # | Уровень | Проблема | Решение | ADR |
|---|---|---|---|---|
| P-10 |  🟢 LOW | Bidirectional `USES_DTO` взрывает граф через ts-common: изменение одного метода → весь репо | `TRAVERSAL_POLICY`: `USES_DTO` только forward (backward=False); backward разрешён только если старт = сам DTO | [ADR-005](adr/ADR-005-traversal-strategy.md) |
| P-11 |  🟡 MEDIUM | `CONTAINS`-хопы (SERVICE→CLASS→METHOD) съедают бюджет depth=6, оставляя только 2 межсервисных прыжка | `CONTAINS`, `IMPLEMENTS`, `IMPORTS` — бесплатные хопы, не уменьшают счётчик `depth` | [ADR-005](adr/ADR-005-traversal-strategy.md) |
| P-12 |  🟡 MEDIUM | NodeId формат нашего вывода не совпадает с gold standard оценки → IoU=0 для всех узлов | Хранить `aliases` (несколько форматов одного ID); нормализатор `normalize_node_id()` по флагу `NODE_ID_FORMAT`; **сверить в первый час** | [ADR-002](adr/ADR-002-graph-schema.md) |
| P-13 |  🟡 MEDIUM | BFS стартовый узел не найден (PARTIAL_PARSED, Lombok метод, неверный формат) → blast radius пуст | Fallback-иерархия старта: точный метод → класс → все классы файла → сервис | [ADR-005](adr/ADR-005-traversal-strategy.md) · [ADR-019](adr/ADR-019-multi-signal-analysis.md) |
| P-14 |  🟢 LOW | `max_depth=6` выбрано произвольно, нет калибровки | Конфигурируемый параметр; **в первый день**: запустить глубины 3–7 на 3-5 change_spec → выбрать по размеру blast radius | [ADR-005](adr/ADR-005-traversal-strategy.md) |
| P-15 |  🟢 LOW | `nx.articulation_points()` на directed + CONTAINS даёт бессмысленные результаты (каждый SERVICE = cut vertex) | Bridge detection только на `CALLS`/`CALLS_SERVICE` рёбрах без CONTAINS; планировать как опцию, не обязательный шаг | [ADR-005](adr/ADR-005-traversal-strategy.md) |
| P-16 |  🟢 LOW | ts-common: BFS взрывает blast radius до 200+ узлов от любого общего DTO | Landmark pre-computation для горячих узлов; semantic ranker прунит по cosine similarity к стартовому узлу | [ADR-005](adr/ADR-005-traversal-strategy.md) |
| P-17 |  🟠 HIGH | Формат `change_spec` от хакатона неизвестен (файл? NodeId? diff?) | Гибкий `resolve_start_nodes()`: нормализует любой из форматов; логировать что распознано — критично для отладки | [ADR-007](adr/ADR-007-api-layer.md) |
| P-84 |  🟢 LOW | `arch_pattern = UNKNOWN` для инфраструктурных сервисов без кода (alertmanager, grafana, elasticsearch, redis и др.) — LLM-агент не отличает «паттерн неизвестен» от «нет исходного кода для анализа» | Два разных значения: `N/A` для узлов с `node_type=INFRA` или SERVICE без дочерних CLASS-узлов; `UNKNOWN` только для SERVICE с кодом но неопределимым паттерном | — |
| P-85 |  🟡 MEDIUM | `IMPLEMENTS` рёбра добавлены в граф, но BFS их не обходит — изменение интерфейса не распространяется на реализующие классы | Добавить `IMPLEMENTS` в `_NO_DEPTH_COST` (бесплатный хоп вперёд); обратный обход: `IMPLEMENTS backward` — если изменился интерфейс, все его реализации затронуты | [ADR-005](adr/ADR-005-traversal-strategy.md) |
| P-94 |  🟢 LOW | Циклические зависимости между сервисами образуют кластеры — BFS не выявляет SCC, blast radius недооценён для узлов внутри цикла | `nx.strongly_connected_components()` O(V+E) после build; SCC-кластеры размером ≥ 2 → добавлять все узлы кластера в blast radius при касании любого из них; `technical_debt[CIRCULAR_CLUSTER]` | [ADR-021](adr/ADR-021-compiler-techniques-debt-recovery.md) |

---

## 3. Конфиги и ENV

| # | Уровень | Проблема | Решение | ADR |
|---|---|---|---|---|
| P-18 |  🟡 MEDIUM | `docker-compose.yml` может отсутствовать — весь конфиг-пайплайн рассыпается | `EnvironmentProfile` Tier 1–4 определяет доступные источники; тактики активируются по Tier | [ADR-019 §A](adr/ADR-019-multi-signal-analysis.md) · [ADR-011](adr/ADR-011-config-file-parsers.md) |
| P-19 |  🟢 LOW | `${VAR:ts-order-service}` — Spring EL с дефолтом: VAR нет в ENV_INDEX, но дефолт есть | Уровень 0 перед строковым литералом: парсим `:default` часть до обращения к ENV_INDEX | [ADR-003 §D.1](adr/ADR-003-fallback-strategy.md) |
| P-20 |  🟡 MEDIUM | Gateway YAML даёт только glob-паттерны (`/api/v1/orderservice/**`), конкретный хендлер неизвестен | RouteIndex: cascade exact → path variables → glob → prefix; gateway YAML парсится первым | [ADR-011](adr/ADR-011-config-file-parsers.md) · [ADR-003 §F](adr/ADR-003-fallback-strategy.md) |
| P-21 |  🟢 LOW | `ORDER_BACKEND` не находится в ENV_INDEX (нестандартное имя переменной) | Fuzzy lookup: BGE-M3 similarity по именам переменных; `ORDER_BACKEND` → `ORDER_SERVICE_URL` (sim=0.83) | [ADR-011](adr/ADR-011-config-file-parsers.md) |
| P-22 |  🟢 LOW | `application-dev.yml` переопределяет `application.yml` — какой использовать? | Мёрж обоих с приоритетом profile-specific; анализировать все профили | [ADR-011](adr/ADR-011-config-file-parsers.md) |
| P-23 |  🟡 MEDIUM | RabbitMQ: отправитель и получатель связаны только именем очереди-константой | Value Flow Graph: трассировка до первого compile-time значения; **стоп-условие** — если VFG упирается в `@Value`/`env.getProperty()`/runtime — значение неизвестно, создать `USES_MAGIC_CALL` (не считать имя); фиксировать только строковый литерал или `static final` константу до того, как цепочка ушла в конфиг | [ADR-003 §E](adr/ADR-003-fallback-strategy.md) · [ADR-002](adr/ADR-002-graph-schema.md) |
| P-24 |  🟢 LOW | ts-voucher-service (Python): порты `ts-order-service:12031` как строки, не service name | Паттерн `hostname:port` → взять hostname → смапить на известный сервис | [ADR-011](adr/ADR-011-config-file-parsers.md) · [ADR-001](adr/ADR-001-parser-strategy.md) |

---

## 4. ML-пайплайн

| # | Уровень | Проблема | Решение | ADR |
|---|---|---|---|---|
| P-25 |  🟢 LOW | Ollama генерирует синтаксически невалидный tree-sitter query → ложные срабатывания на 635 файлах | `ts.Language.query(pattern)` и `re.compile(pattern)` перед сохранением; невалидное → ReAct или SKIP | [ADR-014](adr/ADR-014-ml-pipeline.md) |
| P-26 |  🟢 LOW | `DBSCAN eps=0.15` выбрано произвольно — может дать 1 кластер или 100 одиночек | Логировать кол-во кластеров и одиночек при первом запуске; если < 3 кластеров — снизить eps на 0.05 | [ADR-017](adr/ADR-017-framework-profiles.md) |
| P-27 |  🟢 LOW | BGE-M3 (570MB) + GraphCodeBERT (500MB) + Ollama 7b (5GB) + PostgreSQL + Redis = ~8GB на 16GB UMA | Загружать модели последовательно, не одновременно; BGE-M3 приоритет (нужен для парсинга), GraphCodeBERT — после | [ADR-014](adr/ADR-014-ml-pipeline.md) |
| P-28 |  🟢 LOW | `make db-seed` при первом запуске генерирует эмбеддинги для 500+ примеров — может занять 10+ минут | Предгенерировать эмбеддинги seed-паттернов в git как `seeds/*.npy`; db-seed только загружает их | [ADR-014](adr/ADR-014-ml-pipeline.md) · [ADR-018](adr/ADR-018-unified-rule-store.md) |
| P-29 |  🟢 LOW | Неизвестный Python/Go сервис не классифицируется через ROLE_MAP | `bootstrap_unknown_service()`: requirements.txt → dir structure vector → entrypoint BGE-M3 | [ADR-019 §B/Q4](adr/ADR-019-multi-signal-analysis.md) · [ADR-014](adr/ADR-014-ml-pipeline.md) |
| P-86 |  🟢 LOW | Dense-поиск без ColBERT: recall@10 ~0.87 для gateway/ts-common/INTERFACE-узлов с сотнями входящих рёбер — именно они важны при blast radius | ColBERT только для hot-nodes (`pagerank > 0.01` или `in_degree > 10`): вектор на каждый токен → MaxSim scoring; recall@10 ~0.95; RAM 512 векторов × 1024 × 4 bytes/узел | [ADR-030 §E](adr/ADR-030-multi-granularity-embeddings-bm25-hnsw.md) |

---

## 5. Архитектура и инфраструктура

| # | Уровень | Проблема | Решение | ADR |
|---|---|---|---|---|
| P-30 |  🔴 CRITICAL | 4 микросервиса + Hexagonal + DDD = 3–4 дня на инфраструктуру до первой строки BFS | Монолит по умолчанию (`MONOLITH_MODE=true`): `python -m blast_radius analyze ...` без Docker/PostgreSQL | [ADR-006](adr/ADR-006-engine-architecture.md) · [ADR-009](adr/ADR-009-microservices.md) |
| P-31 |  🟢 LOW | `graph.pkl` загружается при старте engine-api; worker пишет новый pickle → API отдаёт устаревший граф | Redis ключ `graph:snapshot:version` + hot-reload в API без перезапуска | [ADR-005](adr/ADR-005-traversal-strategy.md) · [ADR-008](adr/ADR-008-storage-architecture.md) |
| P-32 |  🟢 LOW | GIL блокирует `ThreadPoolExecutor` для CPU-bound BFS | fork CoW (uvicorn workers): граф shared read-only без копий; `ProcessPoolExecutor` для независимых start nodes | [ADR-005](adr/ADR-005-traversal-strategy.md) · [ADR-007](adr/ADR-007-api-layer.md) |
| P-33 |  🟢 LOW | Новое Ollama-правило не переиндексирует уже обработанные файлы | `rules_version` в `indexed_files`; Redis счётчик инкрементируется при новом правиле → мягкий реиндекс затронутых файлов | [ADR-018](adr/ADR-018-unified-rule-store.md) · [ADR-008](adr/ADR-008-storage-architecture.md) |
| P-34 |  🟢 LOW | Правила разбросаны по 6 местам: ROLE_MAP, SHELL_PATTERNS, env_patterns.yaml, framework profiles, generated_rules, seeds | Единая таблица `rules` в PostgreSQL; все источники → `make db-seed`; runtime только через `RuleEngine` | [ADR-018](adr/ADR-018-unified-rule-store.md) |
| P-87 |  🟡 MEDIUM | GraphBuilder сканирует все файлы всех сервисов при каждом запросе — нет pre-filtering по релевантным кандидатам | PassportRegistry Level-1 BFS (~68 паспортов, 0.08с) даёт кандидатов; Level-2 запускает файловые адаптеры только для 2–5 кандидатов из 39 → 5% загрузки файлов вместо 100%; **нулевой уровень:** ripgrep-список файлов с токенами запроса до PassportRegistry — связан с P-98 | [ADR-033](adr/ADR-033-service-passport-two-level-blast-radius.md) |
| P-88 |  🟢 LOW | `static final BASE + "/path"` — compile-time constant folding даёт готовый URL в `.class`, но Value Flow Graph из source не всегда разворачивает цепочки суммирования строк | `--bytecode-hints <target-dir>`: jawa извлекает строки constant pool → RouteIndex; не заменяет source-анализ, только верифицирует и дополняет | [ADR-026](adr/ADR-026-bytecode-analysis.md) |
| P-93 |  🟠 HIGH | При отсутствии git diff локализовать затронутый сервис трудно: имена сервисов (ts-order-service) не появляются в тексте методов — dense-поиск промахивается по бизнес-токенам | **Bag of Business Tokens + Token Taxonomy:** offline-индексация с 4-уровневой классификацией — (1) Системные (`if`, `return`, `class`) → удалить; (2) Служебные (`controller`, `dto`, `repository`) → `arch_role` узла; (3) Доменные (`ticket`, `order`, `cancel`) → профиль сервиса для BM25+Dense; (4) Инфраструктурные (`kafka`, `postgres`, `mongo`) → `infra_tags` (→ P-99); online: гибридный BM25+Dense (alpha=0.5) по доменным тегам → 1-2 кандидата; offline и online этапы явно разделены | [ADR-033](adr/ADR-033-service-passport-two-level-blast-radius.md) · [ADR-030](adr/ADR-030-multi-granularity-embeddings-bm25-hnsw.md) |
| P-98 |  🟡 MEDIUM | При локализации без известного change_spec нет каскада с early-exit: tree-sitter запускается на все ~2000+ файлов без pre-filtering → медленно и расточительно; Ollama применяется даже там, где tree-sitter хватило бы | **4-уровневый каскад с early-exit:** (0) `ripgrep -l "token1\|token2"` — мгновенно отсекает ~80% файлов без парсинга; (1) PassportRegistry Level-1 + Bag of Tokens (P-93) → 1-2 сервиса-кандидата; (2) tree-sitter детерминированные шаблоны на файлах-кандидатах — 90% покрытие за доли секунды; (3) Ollama только при ≤ 0 результатов после шага 2, с обязательной валидацией ts-query; каждый уровень проверяет результат — если нашёл, следующий не запускается | [ADR-033](adr/ADR-033-service-passport-two-level-blast-radius.md) · [ADR-014](adr/ADR-014-ml-pipeline.md) |
| P-99 |  🟢 LOW | Инфраструктурные токены (`kafka`, `postgres`, `mongodb`) встречаются в разных сервисах, но не используются для выявления потенциальных SHARES_RESOURCE связей — без парсинга конфигов эти связи теряются | При offline-индексации сохранять `infra_tags: [kafka, postgres]` в профиль сервиса (ServicePassport); cross-service матч: сервисы с одинаковыми infra_tags → потенциальная `SHARES_RESOURCE` с `confidence=0.4, source=infra_tag_match`; верификация через конфиг-парсеры повышает conf до 0.9; не создавать рёбра без верификации | [ADR-033](adr/ADR-033-service-passport-two-level-blast-radius.md) · [ADR-011](adr/ADR-011-config-file-parsers.md) |
| P-96 |  🟡 MEDIUM | Нет чёткого разделения in-memory vs персистентного хранилища: векторы иногда остаются в памяти → OOM при росте графа, потеря при рестарте | Архитектурный split: **IN_MEMORY** — NetworkX граф, SymbolIndex, RouteIndex, PassportRegistry (быстрый lookup); **LANCEDB** — все эмбеддинги (emb_struct/emb_semantic/emb_full), service_table, SimilarityIndex векторные колонки; **FILES** — `.npy` seed-эмбеддинги (commit в git, не пересчитывать при каждом старте); никакие векторные данные не должны жить только в `dict` | [ADR-008](adr/ADR-008-storage-architecture.md) · [ADR-030](adr/ADR-030-multi-granularity-embeddings-bm25-hnsw.md) |

---

## 6. Shadow-связи и комментарии

| # | Уровень | Проблема | Решение | ADR |
|---|---|---|---|---|
| P-35 |  🟢 LOW | ts-news-service: весь MongoDB код закомментирован — зависимость не видна | Shadow pipeline: tree-sitter находит comment-узлы → BGE-M3 classifier (порог 0.75) → `SHADOW_SHARES_RESOURCE` ребро; **флаг `include_shadow=False`** передаётся в `GraphBuilder` — при False shadow pipeline не запускается вообще (сборка быстрее, рёбра не добавляются) | [ADR-016](adr/ADR-016-shadow-connections.md) |
| P-36 |  🟢 LOW | Shadow рёбра могут снижать precision если gold standard их не учитывает | Флаг `include_shadow` управляет двумя уровнями: **сборка** (GraphBuilder пропускает shadow pipeline) и **запрос** (BFS игнорирует SHADOW_* рёбра); отдавать оба варианта на первых test cases и зафиксировать лучший | [ADR-016](adr/ADR-016-shadow-connections.md) · [ADR-007](adr/ADR-007-api-layer.md) |
| P-37 |  🟢 LOW | Deprecated и bug-комментарии (`@deprecated`, `FIXME`, `HACK`) несут информацию об узле | Regex-проход отдельно от shadow pipeline: `CommentMetadata`; deprecated → criticality × 0.5; has_bug_note → × 1.3 | [ADR-019 §E](adr/ADR-019-multi-signal-analysis.md) · [ADR-004](adr/ADR-004-node-abstraction-levels.md) |
| P-38 |  🟢 LOW | Javadoc `/** ... */` — не закомментированный код | Исключать явно по токену `/**`; не передавать в BGE-M3 классификатор | [ADR-016](adr/ADR-016-shadow-connections.md) |

---

## 7. Оценка и метрика (IoU)

| # | Уровень | Проблема | Решение | ADR |
|---|---|---|---|---|
| P-39 |  🟢 LOW | Shadow connections включены по умолчанию, но gold standard может их не включать | Флаг `include_shadow=False`; запустить оба варианта на первых test cases, выбрать лучший | [ADR-016](adr/ADR-016-shadow-connections.md) |
| P-40 |  🔴 CRITICAL | Gold standard = пустое множество + мы возвращаем непустое → precision=0, IoU=0 | Обработать edge case: если blast radius > N (настраиваемый порог) → предупреждение; возможно отдавать `[]` | [ADR-005](adr/ADR-005-traversal-strategy.md) |
| P-41 |  🟢 LOW | `CommandLineRunner.run()` в 15+ сервисах создаёт ложные `CALLS_SERVICE` к seed-данным | Класс с `implements CommandLineRunner` → `arch_role=init_data`; вызовы из `run()` → только `CALLS` внутрисервисные | [ADR-003 §D.4](adr/ADR-003-fallback-strategy.md) |
| P-42 |  🟢 LOW | `PollThread.run()` делает RestTemplate-вызовы из `Thread.run()`, не из `@RequestMapping` | Сканировать `run()` метод при наличии RestTemplate-поля в классе; `metadata.call_context=thread_run` | [ADR-003 §D.3](adr/ADR-003-fallback-strategy.md) |

---

---

## 8. Эмбеддинги, паттерны и извлечение информации

| # | Уровень | Проблема | Решение | ADR |
|---|---|---|---|---|
| P-43 |  🟡 MEDIUM | Голое имя метода (`createOrder`) — слабый эмбеддинг, много коллизий | Контекстный шаблон: SERVICE + PKG + ANNOTATIONS + FLOW_STEPS + LOGGER_TAGS + CALLS | [ADR-020 §D](adr/ADR-020-node-taxonomy-embedding-context.md) |
| P-82 |  🟠 HIGH | Dense-поиск по точным идентификаторам (`getServiceUrl`, `QUEUE_EMAIL_SERVICE`) — recall ~70%; конкурент в топ-K из другого сервиса с похожим именем | BM25 гибрид: `alpha×dense + (1−alpha)×sparse`; `_is_identifier_query()` sniffs camelCase/UPPER_CASE → alpha=0.3; BGE-M3 sparse_vecs бесплатны | [ADR-030 §B](adr/ADR-030-multi-granularity-embeddings-bm25-hnsw.md) |
| P-83 |  🟡 MEDIUM | Один вектор на узел: запрос «структурно похожий класс» и «дай контекст для LLM» требуют разных входных текстов → качество одного из них всегда снижается | **Начать с одного вектора** (`emb_semantic`: роль + аннотации + сигнатуры ~400 токенов) — баланс между скелетом и полным контекстом; добавить `emb_struct`/`emb_full` позже если RAM позволяет и recall недостаточен | [ADR-030 §A](adr/ADR-030-multi-granularity-embeddings-bm25-hnsw.md) |
| P-44 |  🟢 LOW | Единый cosine-порог для всех ролей неверен (ENV_PATTERN vs SEMANTIC_SEARCH — разные пространства) | Per-role thresholds: ENV_PATTERN=0.82, SHADOW_CODE=0.75, CODE_STRUCTURE=0.88, SEMANTIC_SEARCH=0.65 | [ADR-020 §C](adr/ADR-020-node-taxonomy-embedding-context.md) |
| P-45 |  🟢 LOW | Паттерны `getServiceUrl({X})` имеют переменные слоты — нельзя хранить как строку | `template` pattern_type + `template_slots` JSONB колонка; `template_match()` через regex-группы | [ADR-020 §B](adr/ADR-020-node-taxonomy-embedding-context.md) · [ADR-018](adr/ADR-018-unified-rule-store.md) |
| P-46 |  🟢 LOW | SERVICE/INTERFACE/DTO изменяются редко но дают огромный blast radius — нет различия с METHOD | Anchor vs Active taxonomy: предупреждение при anchor-старте; разные base criticality по типу | [ADR-020 §A](adr/ADR-020-node-taxonomy-embedding-context.md) |
| P-47 |  🟢 LOW | Комментарии `//1. //2. //3.` в бизнес-методах — бесплатный контекст | `FLOW_STEP_PATTERN` regex; `flow_steps[]` в метаданных узла → контекст эмбеддинга | [ADR-020 §E.1](adr/ADR-020-node-taxonomy-embedding-context.md) |
| P-48 |  🟢 LOW | Logger `[preserve][Step 4][Do Order]` — структурированные метки без парсинга | `LOGGER_TAG_PATTERN` regex; `logger_tags[]` в метаданных → контекст эмбеддинга | [ADR-020 §E.2](adr/ADR-020-node-taxonomy-embedding-context.md) |
| P-49 |  🟢 LOW | Закомментированные `@Value("${service.url}")` в 3+ сервисах — shadow конфиг-зависимость высокой точности | Отдельный под-тип: `SHADOW_CALLS_SERVICE` conf=0.90; не обычный shadow (conf=0.4) | [ADR-020 §E.3](adr/ADR-020-node-taxonomy-embedding-context.md) · [ADR-016](adr/ADR-016-shadow-connections.md) |
| P-50 |  🟢 LOW | `// ts-travel-service:12346/travel/getTripAllDetailInfo` в активном коде — legacy URL-документация | URL hints → RouteIndex обогащение legacy-путями; НЕ shadow pipeline | [ADR-020 §E.4](adr/ADR-020-node-taxonomy-embedding-context.md) |
| P-51 |  🟢 LOW | `// TODO: change to async message service` в preserve/cancel — планируемое async-ребро | `planned_async: true` в метаданных; предупреждение в JSON-выводе для этого ребра | [ADR-020 §E.5](adr/ADR-020-node-taxonomy-embedding-context.md) |
| P-52 |  🟢 LOW | `// This relies on a lot of other services, not completely modified` — неполная зависимость | `has_incomplete_deps: true` → добавить `UnknownDynamicTarget` с весом 1.2 в BFS | [ADR-020 §E.6](adr/ADR-020-node-taxonomy-embedding-context.md) |
| P-53 |  🟠 HIGH | `target/` Maven build artifacts индексируются вместе с исходниками → тысячи лишних файлов | `EXCLUDED_DIRS = {"target", "node_modules", ".gradle", "__pycache__"}` в `iter_source_files()` | [ADR-020 §.github](adr/ADR-020-node-taxonomy-embedding-context.md) · [ADR-019](adr/ADR-019-multi-signal-analysis.md) |
| P-89 |  🟢 LOW | `emb_semantic` одинакового веса для геттера `getId()` и бизнес-метода `preserve()` — геттеры засоряют retrieval, снижая точность поиска | `_classify_method_tier()`: HIGH (`@RestController`, `@Service`, `main()`) → все три emb; MEDIUM (`@Repository`, вызовы restTemplate) → emb_struct + emb_semantic; LOW (геттеры, `toString`, только-logger методы) → только emb_struct или не индексировать | [ADR-030 (чанкинг)](adr/ADR-030-multi-granularity-embeddings-bm25-hnsw.md) |
| P-90 |  🟢 LOW | Узлы графа не имеют топологических метрик (pagerank, in_degree) и quality-сигналов (has_tests, churn_score) — LLM-агент не может оценить «важность» узла без этих данных | `NodeMeta` post-processing: `nx.pagerank(G)` + `in/out_degree`; `has_tests` по паттерну `*Test.java`; `doc_summary` из Javadoc первый параграф / `@ApiOperation`; `churn_score` из `git log` (Tier 3+) | [ADR-030 §F](adr/ADR-030-multi-granularity-embeddings-bm25-hnsw.md) |

---

## 9. Compiler-inspired техники, архитектурные слои, технический долг, восстановление

| # | Уровень | Проблема | Решение | ADR |
|---|---|---|---|---|
| P-54 |  🔴 CRITICAL | `if (tripId.startsWith("G"))` → один URL или другой — без CFG создаём лишнее/неполное CALLS_SERVICE; в хакатоне **branch selection — главный bottleneck**: с правильными entry points IoU = 0.52–0.76, т.е. ветвление остаётся основным потерями | **CFG по минимуму**: только условные CALLS_SERVICE/remote_call — добавлять `cfg_condition` атрибут на ребро; `type="G\|D"` из сценария → выбирать ветку; полный CFG не реализовывать | [ADR-021 §A.1](adr/ADR-021-compiler-techniques-debt-recovery.md) |
| P-55 |  🟢 LOW | `requestUrl` переприсваивается в трёх местах — неясно какое значение достигает restTemplate | Reaching definitions: статический обход AST до USE-точки, без выполнения кода | [ADR-021 §A.2](adr/ADR-021-compiler-techniques-debt-recovery.md) |
| P-56 |  🟢 LOW | `String serviceUrl = url` — переменная-алиас скрывает источник getServiceUrl() | Alias analysis: рекурсивное разворачивание до первичного источника (cap=10) | [ADR-021 §A.3](adr/ADR-021-compiler-techniques-debt-recovery.md) |
| P-57 |  🟢 LOW | `@Autowired OrderService` — CALLS идёт к интерфейсу, а не реализации | Lightweight type inference: SymbolIndex.get_implementors() + соглашение ServiceImpl | [ADR-021 §A.4](adr/ADR-021-compiler-techniques-debt-recovery.md) |
| P-58 |  🟢 LOW | @Transactional(REQUIRES_NEW) изолирует откат — blast radius через это ребро меньше | Сохраняем tx_propagation в атрибутах; предупреждение в warnings[] JSON | [ADR-021 §B.1](adr/ADR-021-compiler-techniques-debt-recovery.md) |
| P-59 |  🟢 LOW | @Async/@Scheduled/@PostConstruct/@HystrixCommand — богатая архитектурная семантика без парсинга | Regex-сбор в RichNodeMetadata; Spring Events → ASYNC_MESSAGE ребро; @Cacheable → SHARES_RESOURCE | [ADR-021 §B](adr/ADR-021-compiler-techniques-debt-recovery.md) |
| P-60 |  🟢 LOW | `rest-service-external:16100` в AsyncTask — вызов несуществующего сервиса → ложное ребро в графе | Dead service ref: сравнить target_service с known_services из docker-compose | [ADR-021 §C.4](adr/ADR-021-compiler-techniques-debt-recovery.md) |
| P-61 |  🟢 LOW | Обработчик @RequestMapping без вызывающих, или вызов без обработчика — односторонняя связь | Dangling caller + Orphan endpoint детекция через RouteIndex cross-check | [ADR-021 §C.1–C.2](adr/ADR-021-compiler-techniques-debt-recovery.md) |
| P-62 |  🟢 LOW | tree-sitter ERROR-узлы на плохих файлах → пропустить весь файл = потерять узлы | Градиент D.0–D.4: ratio < 1% = clean; хирургическое удаление строки; биссекция; javalang; Ollama repair | [ADR-021 §D](adr/ADR-021-compiler-techniques-debt-recovery.md) |

---

## 10. FQN-резолвинг, аномалии в train-ticket (найдено эмпирически)

| # | Уровень | Проблема | Решение | ADR |
|---|---|---|---|---|
| P-63 |  🟡 MEDIUM | **SymbolIndex без FQN**: 5 классов `Queues.java` в разных сервисах с разными значениями `queueName` — non-FQN индекс перезаписывает значения → неверные имена очередей | FQN = package + class + field; resolve через imports файла; same-service fallback; bi-encoder реранкер | [ADR-024 §A](adr/ADR-024-symbol-index-fqn-reranker.md) |
| P-64 |  🟢 LOW | **Wildcard import + одинаковые short names**: `import delivery.config.*` + `Queues.queueName` — не ясно из какого пакета | Bi-encoder реранкер: контекст использования против контекстов объявления; same-service bonus +0.2 | [ADR-024 §B](adr/ADR-024-symbol-index-fqn-reranker.md) |
| P-65 |  🟢 LOW | **CIRCULAR RabbitMQ**: `ts-notification-service` одновременно шлёт и слушает очередь `"email"` → выглядит как dead message | Помечать ребро `is_circular=True`; не считать dead message; включать в `technical_debt[CIRCULAR]` | [ADR-024 §C.1](adr/ADR-024-symbol-index-fqn-reranker.md) |
| P-66 |  🟢 LOW | **Hardcoded IP в datasource**: `${HOST:10.176.122.1}` как дефолт в 5 сервисах — при деплое без переопределения ENV падает подключение к БД | IP-адрес не является именем сервиса → не создавать SHARES_RESOURCE; `missing_db_config=True` + `technical_debt[HARDCODED_IP]` | [ADR-024 §C.2](adr/ADR-024-symbol-index-fqn-reranker.md) |
| P-67 |  🟢 LOW | **ts-consign-mysql только в quickstart compose**: основной `docker-compose.yml` не содержит сервис → зависимый `ts-consign-service` не запустится | Парсить ВСЕ `*docker-compose*.yml` в репо через Two-Phase; объединять ServiceRegistry | [ADR-024 §C.4](adr/ADR-024-symbol-index-fqn-reranker.md) · [ADR-011](adr/ADR-011-config-file-parsers.md) |
| P-68 |  🟢 LOW | **@Value без дефолта и без compose**: `${test_send_mail_user}`, `${swagger.controllerPackage}` — ключ нигде не определён | Если ключ не найден ни в одном шаге lookup chain → `USES_MAGIC_CALL` + `technical_debt[MISSING_CONFIG]` | [ADR-024 §C.3](adr/ADR-024-symbol-index-fqn-reranker.md) · [ADR-023](adr/ADR-023-string-value-lifecycle.md) |

---

---

## 11. Тестовые файлы, моки вне тестов, вложенные комментарии

| # | Уровень | Проблема | Решение | ADR |
|---|---|---|---|---|
| P-69 |  🟢 LOW | Тесты содержат `@Mock RestTemplate` — вызовы внутри теста не являются настоящими зависимостями | Проход 0: `should_skip_file()` исключает `src/test/` — в граф не добавлять; проход 2 (после основного графа): тесты читать только как вторичный источник — извлекать имена сервисов и URL-пути для **подтверждения** уже найденных рёбер (`source=test_hint`), не создавать новые CALLS_SERVICE; связано с P-05 | [ADR-025 §A](adr/ADR-025-test-files-mock-shadow-comments.md) |
| P-70 |  🟢 LOW | MockMvc тесты содержат 250 жёстко прописанных URL-путей — ценные данные для RouteIndex | Извлекать как `test_hint` — подтверждение, не первичный источник; приоритет: gateway > @RequestMapping > test_hint | [ADR-025 §A.3](adr/ADR-025-test-files-mock-shadow-comments.md) |
| P-71 |  🟢 LOW | `// // return x;` — дважды закомментированный код: дерево tree-sitter видит только один `line_comment` | `parse_comment_node()` рекурсивно снимает `//`; если строка внутри уже открытого `//`-блока — это sub-alternative, пропустить (использовать уровень 1); depth-penalty: `conf - (depth-1) × 0.15`; **отложить** до подтверждения ценности shadow pipeline в целом | [ADR-025 §C](adr/ADR-025-test-files-mock-shadow-comments.md) |
| P-74 |  🟢 LOW | `ts-news-service` (Go): `// // db := session.DB("xtest")` внутри полностью закомментированной `getNews()` — ранняя версия кода | Sub-alternative внутри shadow-блока; разбирать только уровень 1: `MONGODB_URL = "ts-news-mongo:27017"` → `SHADOW_SHARES_RESOURCE`; `////...` декоративные разделители и `// // 阿里云...` → пропустить | [ADR-025 §C.1, C.3](adr/ADR-025-test-files-mock-shadow-comments.md) |
| P-72 |  🟢 LOW | `StationFoodServiceImpl.java`: 32 из 121 строк — закомментированные методы с `@Autowired` + `repository.findByTripId()` | Shadow pipeline для методов; `@Autowired` → SHADOW_USES_DTO; `findByTripId` → не граф (внутренний вызов) | [ADR-025 §C.1](adr/ADR-025-test-files-mock-shadow-comments.md) · [ADR-016](adr/ADR-016-shadow-connections.md) |
| P-73 |  🟢 LOW | `old-docs/` содержит 1018 строк подряд `////` (архивный Java) — если не исключить, засорит shadow граф | `old-docs` в `SKIP_PATTERNS` — полное исключение, не анализировать | [ADR-025 §A.2](adr/ADR-025-test-files-mock-shadow-comments.md) |
| P-91 |  🟢 LOW | 250 MockMvc URL-путей в тестах (`MockMvcRequestBuilders.get("/api/v1/...")`) не используются для RouteIndex — ценный дополнительный источник endpoint-контракта | `extract_test_route_hints()` → `source=test_hint`; ниже приоритетом чем `@RequestMapping`; путь только в тесте → `technical_debt[ORPHAN_TEST_URL]` | [ADR-025 §A.3](adr/ADR-025-test-files-mock-shadow-comments.md) |
| P-92 |  🟢 LOW | `// //` внутри закомментированного блока Go (ts-news-service) — sub-alternative, не новая зависимость; обработка наравне с уровнем 1 даёт дубли | `parse_comment_node(depth)`: уровень 2 внутри уже открытого `//`-блока = sub-alternative → брать только уровень 1; `is_decorative_divider()` для `////`; `shadow_conf(base, depth) = base - (depth-1) × 0.15`; **отложить** вместе с P-71 | [ADR-025 §C](adr/ADR-025-test-files-mock-shadow-comments.md) |

---

## 12. Сервисы-родственники и семантика файлов

| # | Уровень | Проблема | Решение | ADR |
|---|---|---|---|---|
| P-75 |  🟡 MEDIUM | `ts-order-service` и `ts-order-other-service` — зеркальные сервисы: изменение в одном требует того же в другом, но граф не знает об этом | **Шаг 1 (rule-based, реализован):** суффикс `-other`/числовой/`-plan`/`-price` → `SIBLING_OF`; **шаг 2 (нечёткое имя):** нормализовать имена сервисов (убрать `ts-`/`-service`) → Левенштейн ratio ≥ 0.8 или trigram cosine ≥ 0.7 — покрывает нестандартные пары; n-граммная инфраструктура уже есть в `DepsAdapter._ngrams()`; **шаг 3 (векторный):** BGE-M3 `emb_struct` ≥ 0.85; если rule-based рёбер мало — шаги 2-3 нецелесообразны | [ADR-027 §B, D](adr/ADR-027-service-siblings-semantic-files.md) |
| P-76 |  🟢 LOW | Условие разделения `trainNumber.startsWith("G\|D")` нигде не фиксируется — теряется архитектурная семантика | `split_condition` атрибут на `SIBLING_OF` ребре; берётся из CFG-анализа (ADR-021) | [ADR-027 §C](adr/ADR-027-service-siblings-semantic-files.md) · [ADR-021](adr/ADR-021-compiler-techniques-debt-recovery.md) |
| P-77 |  🟢 LOW | `OrderServiceImpl` и `OrderOtherServiceImpl` — одинаковая структура, но разные имена → bi-encoder не видит схожести без нормализации | `normalize_class_name()`: убрать domain-prefix, суффиксы `Impl/Other/2`; затем Левенштейн ratio или trigram cosine ≥ 0.7 по нормализованным именам — устойчивее Jaccard для коротких строк; реализовывать только если P-75 шаги 1-2 уже дают достаточно SIBLING рёбер | [ADR-027 §B.2](adr/ADR-027-service-siblings-semantic-files.md) |
| P-78 |  🟢 LOW | Функционально близкие файлы из разных сервисов не связаны в графе — blast radius не распространяется | `FileSimilarityIndex`: кросс-сервисные эмбеддинги файлов; min_sim=0.85; расширение blast radius только для sibling-пар | [ADR-027 §E](adr/ADR-027-service-siblings-semantic-files.md) |
| P-95 |  🟢 LOW | `FileSimilarityIndex` использует попарные cosine-сравнения O(n²) — при >10K файлов (>200 сервисов) время индексирования растёт квадратично | MinHash + LSH: 128-шингловые сигнатуры → `datasketch.MinHashLSH` с порогом Jaccard ≥ 0.5 → O(n) при запросе; путь обновления ADR-031, не замена | [ADR-031](adr/ADR-031-similarity-index.md) · [ADR-027](adr/ADR-027-service-siblings-semantic-files.md) |

---

## 13. UI статика и маршруты

| # | Уровень | Проблема | Решение | ADR |
|---|---|---|---|---|
| P-79 |  🟡 MEDIUM | `ts-ui-dashboard` вызывает сервисы напрямую (минуя getServiceUrl) — без парсинга JS эти рёбра теряются | Angular `$http` вызовы → 49 URL → 27 CALLS_SERVICE рёбер; `extract_ui_routes()` по паттерну `url: "/api/v1/..."` | [ADR-028 §C](adr/ADR-028-ui-static-ajax-routes.md) |
| P-80 |  🟢 LOW | jQuery-файлы (`js/`) вызывают `/cancelOrder`, `/food/getFood` без `/api/v1/` — это не проксируется gateway | Legacy API или Node.js ts-ticket-office-service; добавлять как RouteIndex hint с confidence=0.5, не CALLS_SERVICE | [ADR-028 §A.2](adr/ADR-028-ui-static-ajax-routes.md) |
| P-81 |  🟢 LOW | `ts-avatar-service` (Python) не вызывается через getServiceUrl, но используется из UI — blast radius от avatar не достигает бэкенда | UI-парсер обнаруживает `/api/v1/avatar` в upload.js → добавляет CALLS_SERVICE ребро ts-ui-dashboard → ts-avatar-service | [ADR-028 §B](adr/ADR-028-ui-static-ajax-routes.md) |

---

## 14. Execution Trace Prediction — специфика хакатона

> Задача хакатона — не построение графа с нуля, а **предсказание execution trace** по сценарию.
> Скелет (`trainticket.json.gz`, 15 229 узлов / 19 573 рёбра) уже предоставлен организаторами как **may-граф** (все возможные пути).
> Нужно выбрать реально выполнившиеся рёбра из четырёх типов: `executes`, `calls`, `remote`, `data`.

| # | Уровень | Проблема | Решение | ADR |
|---|---|---|---|---|
| P-100 | 🔴 CRITICAL | `remote = 0.0` у baseline NLP — 415 `remote_call` рёбер в скелете, ни одно не предсказано; 1/4 итогового IoU = 0 | Для каждого метода с `remote_call` рёбрами в скелете: извлечь целевой сервис из `deployment_unit` целевого узла; предсказывать ребро если (а) сервис определён по сценарию-шагу или (б) метод уже в `executes`-множестве → добавить все исходящие `remote_call`; baseline_closure.py показывает паттерн | — |
| P-101 | 🟠 HIGH | `JWTFilter#doFilterInternal` выполняется при каждом HTTP-запросе, но **нет** `calls`-ребра от фильтра к обработчику — Spring вызывает filter chain, не сам метод | JWTFilter входит в `executes` для каждого шага; ребро `calls(JWTFilter → handler)` **не создавать** — Spring dispatches; в скелете нет `calls`-ребра filter→handler | — |
| P-102 | 🟠 HIGH | Состояние БД и контекст данных влияют на выбор ветки: `if (account.getBalance() >= price)` → разные пути при балансе 10 000 vs 0; `if (order.getStatus() == PAID)` → cancel vs error | Хардкодить контекст сценария: дата = 1 октября 2026, логин = `fdse_microservice`, баланс = 10 000; при анализе ветвления использовать как оракул условий — достаточно выбрать правдоподобную ветку | — |
| P-103 | 🟡 MEDIUM | ORM lazy loading: `findAll()` в методе А, данные реально читаются при commit в методе Б — непонятно, где ставить `data`-ребро | `data`-ребро ставится к методу **с вызовом репозитория** (`findAll`, `save`, `delete`), не к методу транзакционного commit; следовать той же конвенции, что в gold standard | — |
| P-104 | 🟡 MEDIUM | `ts-common` содержит shared DTO и утилиты, методы которых вызываются из многих сервисов; в скелете `method:ts-common:...` — один ID на все сервисы | Методы ts-common включаются в `executes`/`calls` как `method:ts-common:...` (не `method:ts-order-service:common...`); при обходе скелета использовать `module` поле узла, не угадывать сервис | — |
| P-105 | 🟡 MEDIUM | `scenario:S0` — псевдо-ID источника для `executes`-рёбер; его нет в скелете как вершины; не путать с реальным NodeId | Источник всех `executes` рёбер в submission = `"scenario:S0"` (или `"scenario:S1"` и т.д.); это literal string в JSON, не NodeId из скелета; `data` и `calls` рёбра имеют метод как источник | — |
| P-106 | 🟠 HIGH | `input: "text"` в сценариях — нет URL, только текстовое описание действия; entry point нужно определять по тексту: "Search regular trains from Shanghai to Nanjing" → `/api/v1/travelservice/trips/left` | NLP-локализация: набор ключевых слов (`search`, `login`, `book`, `pay`, `cancel`, `rebook`) → словарь step_type → endpoint; уточнять по `ts-ui-dashboard` Angular-роутам; baseline_nlp.py использует BM25 по method-сигнатурам | — |
| P-107 | 🟡 MEDIUM | Lombok-геттеры, конструкторы, `equals`/`hashCode` вызываются через Spring/Hibernate рефлексию — включать в `executes` или нет? | **Не включать**: согласно правилам хакатона, методы сгенерированные Lombok и вызванные через рефлексию (не прямым Java-вызовом) исключаются из `executes` и `calls`; только явные вызовы в source code | — |
| P-108 | 🟡 MEDIUM | Дочерние таблицы коллекций (`user_roles`, `route_stations`, `route_distances`) — отдельные `data_resource` вершины в скелете; JPA `@ElementCollection` / `@OneToMany` не имеет явного SQL в source | Если репозиторий делает `findById(User)` и у User есть `@ElementCollection roles` → добавлять `data`-ребро к `data_resource:user_roles` так же как к `data_resource:user`; искать в скелете вершины с именем `{table}_{collection_field}` | — |

---

## Быстрая сводка: топ-10 по влиянию на IoU

| Приоритет | ID | Краткое описание |
|---|---|---|
| 🔴 CRITICAL | P-54 | Branch selection — главный bottleneck; даже с правильными entry points IoU только 0.52–0.76 |
| 🔴 CRITICAL | P-100 | remote=0.0 у baseline: 415 remote_call рёбер в скелете, 1/4 IoU = 0 |
| 🔴 CRITICAL | P-40 | gold remote = 0 в S0: любое предсказание remote → IoU=0; точность важнее recall |
| 🔴 CRITICAL | P-30 | Монолит first: без рабочего пайплайна нет submission |
| 🟠 HIGH | P-106 | input=text: нет URL, NLP-локализация entry point по тексту шага |
| 🟠 HIGH | P-93 | Token Taxonomy — ключ к NLP entry point локализации |
| 🟠 HIGH | P-82 | BM25 hybrid — поиск entry points по тексту сценария; recall ~70% без гибрида |
| 🟠 HIGH | P-101 | JWTFilter: executes но нет calls к handler — правило для каждого шага |
| 🟠 HIGH | P-102 | DB context (баланс, статус заказа) как оракул для branch selection |
| 🟠 HIGH | P-01 | getServiceUrl cross-file: нужен для branch analysis (order vs order-other) |
| 🟠 HIGH | P-17 | Формат входного сценария: steps[] с текстом — ключевой input |
| 🟡 MEDIUM | P-12 | NodeId из скелета — сопоставление при формировании submission |
| 🟡 MEDIUM | P-75 | ts-order vs ts-order-other: branch по trainNumber.startsWith("G\|D") |
