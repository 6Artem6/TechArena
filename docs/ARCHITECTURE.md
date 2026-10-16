# Архитектура системы

**Связанные:** [ADR-006](adr/ADR-006-engine-architecture.md), [ADR-009](adr/ADR-009-microservices.md), [HOW_IT_WORKS.md](HOW_IT_WORKS.md)

---

## Сервисная топология

```mermaid
graph TB
    subgraph external ["Внешние клиенты"]
        USER[Пользователь]
        CLI[CLI\nотладка]
    end

    subgraph frontend ["ui-service :3000"]
        REACT[React SPA\nVite + sigma.js]
        NGINX[nginx\nстатика + прокси]
    end

    subgraph api ["engine-api :8000"]
        FASTAPI[FastAPI\nasync]
        ROUTERS[routers/\ngraph, analyze, index, health]
        USECASES[application/\nuse cases]
    end

    subgraph workers ["worker-service"]
        ARQ[arq worker\nасинхронный]
        POOL[ProcessPoolExecutor\nCPU парсинг]
        PARSERS[Parsers\ntree-sitter + javalang]
    end

    subgraph ml ["ml-service :8001\nstateless"]
        EMBED[embeddings\nGraphCodeBERT / BGE-M3]
        OLLAMA[Ollama\nQwen-Coder]
    end

    subgraph storage ["Хранилища"]
        PG[(PostgreSQL\n+ pgvector\nузлы, рёбра, векторы)]
        REDIS[(Redis\nочереди + кэш)]
    end

    USER --> NGINX
    CLI --> FASTAPI
    NGINX --> REACT
    NGINX -->|/api proxy| FASTAPI
    FASTAPI --> ROUTERS
    ROUTERS --> USECASES
    USECASES -->|read| PG
    USECASES -->|enqueue| REDIS

    REDIS -->|tasks| ARQ
    ARQ --> POOL
    POOL --> PARSERS
    ARQ -->|write nodes/edges/vectors| PG
    ARQ -->|progress| REDIS
    ARQ -->|embed/summarize| EMBED
    ARQ -->|summarize| OLLAMA
    EMBED -->|vectors| ARQ
    OLLAMA -->|summaries| ARQ
```

---

## Слои архитектуры (Hexagonal)

```mermaid
classDiagram
    class Domain {
        model: Node, Edge, DependencyGraph, ChangeSpec
        services: BlastRadiusCalculator, NodeCriticality
        ports: CodeParser, GraphRepository, VectorStore, CachePort
    }

    class Adapters {
        parsers: TreeSitterJava, JavalangFallback, PythonAst, GoParser, NodeJs
        graph: NetworkXRepository + PickleStore (Local); PostgresGraphRepository (Deferred)
        vector: LanceDBStore — 3 гранулярности: emb_struct/emb_semantic/emb_full + sparse BM25
        cache: PickleCache (Local); RedisCacheAdapter (Deferred)
        output: JsonSerializer
        tools_code: SemgrepAdapter (30+ langs), CtagsAdapter (100+ langs)
        tools_config: NginxAdapter, YamlAdapter (Helm/k8s/compose), DockerAdapter
                      DotenvAdapter, PropertiesAdapter, DepsAdapter
        protocols: _protocols.parse_url() — 35+ протоколов (ws/jdbc/amqp/redis/...)
    }

    class Application {
        BuildGraphUseCase
        AnalyzeBlastRadiusUseCase
        IndexStatusUseCase
    }

    class Infrastructure {
        WorkerPool (ProcessPoolExecutor + asyncio)
        FileWalker
        ConfigLoader
        DB connections (connections.py)
        CLI (typer)
    }

    class API {
        FastAPI app
        routers: graph, analyze, index, health
        dto: requests, responses (Pydantic v2)
        dependencies: get_db, get_redis, get_graph_service
    }

    Domain <|-- Adapters : implements ports
    Adapters <-- Application : uses
    Domain <-- Application : orchestrates
    Application <-- Infrastructure : wires
    Application <-- API : calls use cases
```

---

## Структура репозитория

```
blast-radius-engine/
│
├── services/
│   ├── engine-api/             # FastAPI + domain + adapters
│   │   ├── src/
│   │   │   ├── domain/         # Чистая бизнес-логика
│   │   │   ├── adapters/       # Реализации портов
│   │   │   ├── application/    # Use cases
│   │   │   ├── infrastructure/ # Workers, FileWalker, connections
│   │   │   └── api/            # FastAPI: routers, dto, main, deps
│   │   ├── Dockerfile
│   │   └── README.md
│   │
│   ├── worker-service/         # arq workers + BuildGraph
│   │   ├── src/
│   │   ├── Dockerfile
│   │   └── README.md
│   │
│   ├── ml-service/             # Stateless: embed + summarize
│   │   ├── src/
│   │   ├── Dockerfile
│   │   └── README.md
│   │
│   └── ui-service/             # React + Vite + nginx
│       ├── src/
│       ├── Dockerfile
│       └── README.md
│
├── docs/                       # Документация
│   ├── README.md               # ← Реестр (вы здесь)
│   ├── HOW_IT_WORKS.md
│   ├── ARCHITECTURE.md
│   ├── PLAN.md
│   ├── CHANGELOG.md
│   └── adr/                    # ADR-001 — ADR-013
│
├── docker-compose.yml
├── Makefile
├── pyproject.toml
└── requirements-dev.txt
```

---

## Поток данных при индексации

```mermaid
flowchart LR
    REPO[train-ticket\n47 сервисов\n532 java файла] -->|FileWalker| FILES[Список файлов\nпо сервисам]
    FILES -->|ProcessPoolExecutor| PARSE[Параллельный\nпарсинг]
    PARSE -->|tree-sitter / javalang| NODES_EDGES[Узлы + Рёбра]
    NODES_EDGES -->|batch INSERT| PG[(PostgreSQL)]
    NODES_EDGES -->|AST-сигнатуры| ML[ml-service]
    ML -->|vectors| VECTORS[Эмбеддинги]
    VECTORS -->|UPDATE embedding| PG
```

---

## Поток данных при запросе

```mermaid
flowchart LR
    CS[change_spec] -->|resolve start node| START[Стартовый узел\nin PostgreSQL]
    START -->|BFS двунаправленный| VISIT[visited set]
    VISIT -->|SELECT details| RESULT[Детали узлов]
    RESULT -->|JSON serialize| OUT[stdout: чистый JSON]
    VISIT -->|cache| REDIS[(Redis\nTTL 1h)]
```
