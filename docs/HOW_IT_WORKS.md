# Как работает Blast Radius Engine

**Связанные:** [ADR-003](adr/ADR-003-fallback-strategy.md), [ADR-005](adr/ADR-005-traversal-strategy.md), [ADR-006](adr/ADR-006-engine-architecture.md), [ADR-009](adr/ADR-009-microservices.md)

Движок решает одну задачу: по описанию изменения в коде (`change_spec`) вернуть множество всех узлов системы, которые это изменение затронет — **Blast Radius**.

---

## Два главных потока

```mermaid
flowchart LR
    A[Репозиторий\ntrain-ticket] -->|1. Индексация| B[(PostgreSQL\nграф)]
    C[change_spec] -->|2. Запрос| D[Blast Radius\nJSON]
    B --> D
```

**Поток 1 — Индексация** выполняется один раз (или при обновлении репо): строит граф зависимостей и сохраняет в NetworkX + pickle (Local Mode).

**Поток 2 — Запрос** выполняется при каждом запросе: загружает граф из pickle, запускает BFS от изменённого узла, возвращает затронутые узлы.

> **Local Mode** (текущий): NetworkX + pickle; `python -m blast_radius analyze ... | jq .` — без Docker/PostgreSQL/Redis.  
> **Production Mode** (Deferred): PostgreSQL + pgvector + Redis + FastAPI — ADR-007–010.

---

## Поток 1: Индексация репозитория

### Последовательность

```mermaid
sequenceDiagram
    actor User
    participant API as engine-api
    participant Redis
    participant Worker as worker-service
    participant Parser as Parsers\n(tree-sitter)
    participant ML as ml-service\n(stateless)
    participant PG as PostgreSQL

    User->>API: POST /api/v1/index/repository\n{"repo_path": "./train-ticket"}
    API->>Redis: LPUSH worker:queue:parse {task_id, repo_path}
    API-->>User: 202 Accepted {task_id}

    Redis-->>Worker: BRPOP task
    Worker->>Worker: FileWalker → 47 сервисов

    loop Для каждого сервиса (параллельно, ProcessPoolExecutor)
        Worker->>Parser: parse_service(service_dir)
        Parser->>Parser: tree-sitter Java AST
        Parser->>Parser: Извлечь классы, методы, аннотации
        Parser->>Parser: Найти RestTemplate вызовы → RouteIndex
        Parser-->>Worker: List[Node] + List[Edge]
    end

    Worker->>PG: INSERT nodes batch
    Worker->>PG: INSERT edges batch

    opt Фаза 3: семантический слой
        Worker->>ML: POST /embed/batch [AST-сигнатуры]
        ML-->>Worker: List[vector[768]]
        Worker->>PG: UPDATE nodes SET embedding = ...
    end

    opt Фаза 3: обогащение метаданных
        Worker->>ML: POST /summarize/batch [snippets]
        ML-->>Worker: List[{summary, domain_role}]
        Worker->>PG: UPDATE nodes SET metadata = ...
    end

    Worker->>Redis: SET index:status:{task_id} {status: done}
    User->>API: GET /api/v1/index/status/{task_id}
    API-->>User: {status: done, stats: {nodes: 3421, edges: 8934}}
```

### Tool Adapter Layer — конфиги и зависимости

До запуска языковых парсеров `ToolRegistry` сканирует конфиги репозитория:

```
Репо
 ├── docker-compose.yml   → YamlAdapter    → SERVICE_DEF, ENV_ACCESS
 ├── **/nginx.conf        → NginxAdapter   → PROXY_ROUTE, listen-порты
 ├── **/Dockerfile        → DockerAdapter  → SERVICE_DEF + EXPOSE-порты
 ├── **/.env              → DotenvAdapter  → ENV_ACCESS + URLs (redis://, amqp://...)
 ├── **/application.properties → PropertiesAdapter → CALLS_DB, ASYNC_CALL
 └── **/pom.xml           → DepsAdapter    → IMPORT + tech_hint (db:mysql, mq:rabbitmq)
```

`_protocols.py` парсит 35+ протоколов (`ws`, `wss`, `jdbc:*`, `amqp`, `kafka`, `nats`, …) — везде.  
Все `Finding` попадают в единый `AnalysisResult` → JSON.

### Что происходит внутри парсера

```mermaid
flowchart TD
    F[Java-файл] --> TS{tree-sitter\nпарсинг}
    TS -->|OK| AST[AST-дерево]
    TS -->|ошибка| JL[javalang\nfallback]
    JL -->|OK| AST
    JL -->|ошибка| PP[PARTIAL_PARSED\nузел-заглушка]

    AST --> CLS[Классы + интерфейсы\n@Service @Controller ...]
    AST --> MET[Методы + сигнатуры]
    AST --> ANN[Аннотации\n@RequestMapping ...]
    AST --> IMP[Импорты]
    AST --> REST[RestTemplate вызовы]

    REST --> RES{Резолвинг\nцепочка ADR-003}
    RES -->|литерал| SVC[ребро CALLS_SERVICE\nк конкретному сервису]
    RES -->|@Value| ENV[ENV_INDEX lookup\nдокер-компоуз]
    RES -->|динамика| UNK[UnknownDynamicTarget\nUSES_MAGIC_CALL]

    CLS --> N[Узлы графа]
    MET --> N
    ANN --> N
    SVC --> E[Рёбра графа]
    IMP --> E
    UNK --> E
```

---

## Поток 2: Запрос Blast Radius

### Последовательность

```mermaid
sequenceDiagram
    actor User
    participant CLI as CLI / UI
    participant API as engine-api
    participant Cache as Redis\n(кэш)
    participant UC as AnalyzeBlastRadius\nUseCase
    participant PG as PostgreSQL
    participant BFS as BlastRadiusCalculator\n(domain)

    User->>CLI: change_spec:\n{file: "ts-order-service/.../OrderServiceImpl.java",\n method: "createOrder"}

    CLI->>API: POST /api/v1/analyze/blast-radius

    API->>Cache: GET blast_radius:{hash(change_spec)}
    alt Кэш попал
        Cache-->>API: JSON результат
        API-->>CLI: 200 {affected_nodes: [...]}
    else Кэш пуст
        API->>UC: analyze(change_spec)
        UC->>PG: SELECT node WHERE file = ... AND method = ...
        PG-->>UC: start_node: ts-order-service::order.OrderServiceImpl#createOrder

        UC->>BFS: bfs(start_node, graph, max_depth=6)

        loop BFS: двунаправленный обход
            BFS->>PG: SELECT neighbors(node_id)\n(все типы рёбер: in + out)
            PG-->>BFS: List[Node]
            BFS->>BFS: добавить в visited,\nесли глубина < max_depth
        end

        BFS-->>UC: Set[NodeId] (12 узлов)
        UC->>PG: SELECT node details WHERE id IN (...)
        PG-->>UC: List[Node с метаданными]

        UC->>Cache: SET blast_radius:{hash} JSON TTL=3600
        UC-->>API: BlastRadius result
        API-->>CLI: 200 {affected_nodes: [...], total_count: 12}
    end
```

### Алгоритм BFS внутри

```mermaid
flowchart TD
    S[Стартовые узлы\nиз change_spec] --> Q[Очередь BFS\n+ visited set]

    Q --> N{Узел из очереди}
    N -->|depth >= max_depth| SKIP[пропустить]
    N -->|depth < max_depth| NEIGHBORS[Получить соседей\nвсе типы рёбер\nin + out]

    NEIGHBORS --> FILTER{Уже\nvisited?}
    FILTER -->|да| SKIP
    FILTER -->|нет| ADD[Добавить в visited\nи очередь]
    ADD --> N

    SKIP --> DONE{Очередь\nпуста?}
    DONE -->|нет| N
    DONE -->|да| RES[Set-visited\n= Blast Radius]
```

---

## Как данные проходят через слои архитектуры

```mermaid
flowchart LR
    subgraph HTTP ["HTTP / CLI (транспорт)"]
        REQ[ChangeSpecRequest\nPydantic v2]
    end

    subgraph APP ["Application Layer (use cases)"]
        UC[AnalyzeBlastRadiusUseCase]
    end

    subgraph DOM ["Domain (чистый Python)"]
        CS[ChangeSpec\nvalue object]
        BFS[BlastRadiusCalculator]
        BR[BlastRadius\nvalue object]
    end

    subgraph ADAPT ["Adapters (инфраструктура)"]
        REPO[PostgresGraphRepository]
        CACHE[RedisCacheAdapter]
    end

    subgraph OUT ["Output"]
        JSON[BlastRadiusResponse\nPydantic v2 → JSON stdout]
    end

    REQ -->|model_validate| UC
    UC -->|to_domain| CS
    UC --> BFS
    BFS <-->|find_neighbors| REPO
    UC <-->|get/set| CACHE
    BFS --> BR
    BR -->|from_domain| JSON
```

**Правило:** домен ничего не знает о HTTP, PostgreSQL или Redis. Конвертация — на границах слоёв.

---

## Структура данных в PostgreSQL

```mermaid
erDiagram
    NODES {
        text id PK
        text display_name
        text node_type
        text service
        text arch_role
        text file_path
        jsonb metadata
        vector embedding
        timestamptz created_at
    }

    EDGES {
        bigserial id PK
        text source_id FK
        text target_id FK
        text edge_type
        float weight
        jsonb metadata
    }

    NODES ||--o{ EDGES : "source"
    NODES ||--o{ EDGES : "target"
```

---

## Пример: что происходит при изменении `OrderServiceImpl#createOrder`

```
change_spec:
  file:   ts-order-service/src/main/java/order/service/OrderServiceImpl.java
  class:  order.OrderServiceImpl
  method: createOrder

Стартовый узел:
  ts-order-service::order.OrderServiceImpl#createOrder  (METHOD)

Обход (BFS, max_depth=6):

depth=1 (вверх CONTAINS):
  → ts-order-service::order.OrderServiceImpl  (CLASS)

depth=1 (вниз CALLS из метода):
  → ts-order-service::order.OrderRepository#findByTravelDate  (METHOD)

depth=1 (вбок CALLS_SERVICE):
  → ts-station-service::stationinfo.StationInfoController#queryForNameList  (METHOD)

depth=2 (вверх от StationInfoController):
  → ts-station-service::stationinfo.StationInfoController  (CLASS)
  → ts-station-service  (SERVICE)

depth=2 (вверх от OrderServiceImpl):
  → ts-order-service  (SERVICE)

depth=2 (вверх от OrderRepository):
  → ts-order-service::order.OrderRepository  (CLASS/INTERFACE)

Итого: 8 узлов
Blast Radius: {
  "ts-order-service::order.OrderServiceImpl#createOrder",
  "ts-order-service::order.OrderServiceImpl",
  "ts-order-service::order.OrderRepository#findByTravelDate",
  "ts-station-service::stationinfo.StationInfoController#queryForNameList",
  "ts-station-service::stationinfo.StationInfoController",
  "ts-station-service",
  "ts-order-service",
  "ts-order-service::order.OrderRepository"
}
```

---

## Связанные документы

- [ADR-002](adr/ADR-002-graph-schema.md) — типы узлов и рёбер
- [ADR-003](adr/ADR-003-fallback-strategy.md) — как разрешаются межсервисные вызовы
- [ADR-005](adr/ADR-005-traversal-strategy.md) — параметры BFS и обоснование `max_depth=6`
- [ADR-008](adr/ADR-008-storage-architecture.md) — схема PostgreSQL, Redis-очереди
- [ADR-009](adr/ADR-009-microservices.md) — какой сервис что делает
- [ADR-011](adr/ADR-011-config-file-parsers.md) — RouteIndex и ENV_INDEX
- [ARCHITECTURE.md](ARCHITECTURE.md) — статичная схема компонентов
