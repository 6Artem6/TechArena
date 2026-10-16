# ADR-006: Архитектура движка (Hexagonal + DDD + Async Workers)

**Статус:** Принято  
**Дата:** 2026-10-13

## Контекст

Движок должен быть масштабируемым, лёгким, быстрым и отказоустойчивым. Анализ 500+ файлов в монорепо однопоточно занимает 20–60 секунд. Нужна архитектура, которая:
- Легко тестируется (домен без внешних зависимостей).
- Расширяется без переписывания (новый язык = новый адаптер, не трогаем домен).
- Не падает при проблемах с одним файлом или сервисом.
- Параллельно парсит сервисы.

## Решение

### Гексагональная архитектура (Ports & Adapters) + DDD

```
┌─────────────────────────────────────────────────────┐
│                    DOMAIN                            │
│  model/: DependencyGraph, Node, Edge, ChangeSpec    │
│  services/: BlastRadiusCalculator, Criticality      │
│  ports/: CodeParser, GraphRepository, VectorStore   │
└────────────────┬───────────────────────┬────────────┘
                 │ implements             │ implements
    ┌────────────▼────────┐   ┌──────────▼──────────────┐
    │   ADAPTERS (in)     │   │   ADAPTERS (out)         │
    │  TreeSitterParser   │   │  NetworkXRepository      │
    │  JavalangFallback   │   │  LanceDBVectorStore      │
    │  PythonAstParser    │   │  JsonOutputSerializer    │
    │  NodeJsParser       │   │  PicklePersistence       │
    └────────────┬────────┘   └──────────┬──────────────┘
                 │                        │
    ┌────────────▼────────────────────────▼──────────────┐
    │              APPLICATION LAYER                      │
    │  BuildGraphUseCase   AnalyzeBlastRadiusUseCase     │
    │  (оркестрирует воркеры, агрегирует результат)      │
    └──────────────────────┬──────────────────────────── ┘
                           │
    ┌──────────────────────▼──────────────────────────── ┘
    │              INFRASTRUCTURE                         │
    │  WorkerPool (asyncio + ProcessPoolExecutor)        │
    │  FileWalker, ConfigLoader, CLI                     │
    └─────────────────────────────────────────────────── ┘
```

### Структура директорий

```
src/
├── domain/
│   ├── model/
│   │   ├── node.py           # NodeId (value object), Node, NodeType
│   │   ├── edge.py           # Edge, EdgeType, EdgeWeight
│   │   ├── graph.py          # DependencyGraph (aggregate root)
│   │   └── change_spec.py    # ChangeSpec (value object)
│   ├── services/
│   │   ├── blast_radius.py   # BlastRadiusCalculator
│   │   └── criticality.py    # NodeCriticality (фаза 4)
│   └── ports/
│       ├── code_parser.py    # Protocol: parse_file → List[Node, Edge]
│       ├── graph_repo.py     # Protocol: save/load DependencyGraph
│       └── vector_store.py   # Protocol: index/search (фаза 3)
│
├── adapters/
│   ├── parsers/
│   │   ├── base.py           # BaseParser (ABC)
│   │   ├── treesitter_java.py
│   │   ├── javalang_fallback.py
│   │   ├── python_ast.py
│   │   └── nodejs_parser.py
│   ├── graph/
│   │   ├── networkx_repo.py  # NetworkX impl GraphRepository
│   │   └── pickle_store.py   # Сериализация/десериализация графа
│   ├── vector/
│   │   └── lancedb_store.py  # LanceDB impl VectorStore
│   └── output/
│       └── json_serializer.py
│
├── application/
│   ├── build_graph.py        # UseCase: обход файлов + воркеры + сборка графа
│   └── analyze_blast_radius.py  # UseCase: change_spec → JSON
│
├── infrastructure/
│   ├── worker_pool.py        # ProcessPoolExecutor + asyncio coordination
│   ├── file_walker.py        # Обход файлов с фильтрацией
│   ├── config_loader.py      # docker-compose, application.yml
│   └── cli.py                # Точка входа (argparse)
│
└── scripts/                  # Существующие скрипты (не трогаем)
```

### Модель параллелизма

```
BuildGraphUseCase
    │
    ├── FileWalker → [service_dir_1, service_dir_2, ..., service_dir_47]
    │
    └── WorkerPool.map(parse_service, services)
           │
           ├── Worker 1: parse ts-order-service    → [(nodes, edges)]
           ├── Worker 2: parse ts-preserve-service → [(nodes, edges)]
           ├── Worker 3: parse ts-travel-service   → [(nodes, edges)]
           └── ...
           │
           └── Aggregate: graph.add_nodes_from(...) + graph.add_edges_from(...)
```

- Парсинг CPU-bound (tree-sitter) → `ProcessPoolExecutor`.
- I/O (чтение файлов, конфиги) → `asyncio` + `aiofiles`.
- Граф строится в главном процессе (нет concurrent writes).
- Каждый воркер изолирован: сбой одного сервиса не роняет остальные.

### DDD-словарь

| Термин | Тип | Описание |
|---|---|---|
| `DependencyGraph` | Aggregate Root | Весь граф проекта |
| `Node` | Entity | Узел с уникальным `NodeId` |
| `NodeId` | Value Object | Иммутабельный ID: `{service}::{fqn}#{member}` |
| `Edge` | Value Object | Ребро: (source, target, EdgeType, weight) |
| `ChangeSpec` | Value Object | Описание изменения: файл + класс + метод |
| `BlastRadius` | Value Object | Результат: `Set[NodeId]` |
| `CodeParser` | Port (in) | Интерфейс парсера файла |
| `GraphRepository` | Port (out) | Интерфейс хранилища графа |

## Обоснование

- **Гексагональная архитектура**: домен не зависит от NetworkX, tree-sitter или LanceDB. Можно поменять любой адаптер (например, заменить pickle на SQLite) не трогая бизнес-логику.
- **DDD**: `DependencyGraph` как aggregate root гарантирует целостность инвариантов (нет дублирующихся узлов, корректные типы рёбер).
- **ProcessPoolExecutor**: tree-sitter парсит файлы параллельно; на 8 ядрах 47 сервисов — ~3–5 секунд вместо ~40 секунд.
- **Fail-safe изоляция**: каждый воркер в своём процессе; исключение в одном сервисе не роняет остальные.

## Связанные ADR

**Алгоритм (приоритет):**
- **ADR-001**: Стратегия парсинга (tree-sitter + javalang fallback)
- **ADR-011**: Конфиг-парсеры — docker-compose, Helm, sh, env, RouteIndex
- **ADR-022**: ConceptRegistry + Framework Profiles (language-agnostic)
- **ADR-023**: Value lifecycle + SymbolIndex (статические константы)

**Post-Competition (Deferred):**
- **ADR-007**: FastAPI + Pydantic v2 DTO *(Deferred)*
- **ADR-008**: PostgreSQL/pgvector + Redis *(Deferred)*
- **ADR-009**: Микросервисная топология *(Deferred)*
- **ADR-010**: Docker Compose + React UI *(Deferred)*
- **ADR-012**: Визуализация *(Deferred)*

## Уточнённая структура проекта

```
blast-radius-engine/
├── services/
│   ├── engine-api/       # FastAPI + domain + adapters
│   │   └── src/
│   │       ├── domain/
│   │       ├── adapters/
│   │       ├── application/
│   │       ├── infrastructure/
│   │       └── api/          # routers, dto, main, dependencies
│   ├── worker-service/   # arq workers + BuildGraph UseCase
│   ├── ml-service/       # embeddings, Ollama
│   └── ui-service/       # React + Vite + nginx
├── docs/adr/
├── Makefile              # Единая точка входа
└── docker-compose.yml

```

## Local Mode — MVP для хакатона

ADR-007–010 описывают production-стек (PostgreSQL, Redis, микросервисы, React UI) — всё отложено (Deferred). Для CLI-инструмента с выводом JSON в stdout достаточно **Local Mode**:

```
blast-radius-engine/
├── src/
│   ├── domain/          # DependencyGraph, Node, Edge, ChangeSpec (без изменений)
│   ├── adapters/
│   │   ├── parsers/     # TreeSitterParser, JavalangFallback, PythonAst, NodeJs
│   │   ├── graph/
│   │   │   ├── networkx_repo.py   # NetworkX in-memory (вместо PostgreSQL)
│   │   │   └── pickle_cache.py    # Кеш графа между запусками (вместо Redis)
│   │   └── output/
│   │       └── json_serializer.py # stdout JSON (вместо API endpoint)
│   ├── application/
│   │   ├── build_graph.py
│   │   └── analyze_blast_radius.py
│   └── infrastructure/
│       ├── file_walker.py
│       ├── config_loader.py
│       └── cli.py           # python -m blast_radius analyze <file>
├── docs/adr/
└── Makefile
```

**Адаптеры Local Mode vs Production:**

| Порт | Local Mode | Production (Deferred) |
|---|---|---|
| `GraphRepository` | `NetworkXRepository` (in-memory) | `PostgreSQLRepository` (asyncpg) |
| `VectorStore` | `LanceDBStore` (файл) | `PgvectorStore` (PostgreSQL) |
| `TaskQueue` | Нет (синхронно) | `ArqRedisQueue` |
| `OutputPort` | `JsonStdout` | `FastAPIResponse` |
| `Cache` | `PickleCache` (файл) | `RedisCache` |

**Запуск:**
```bash
python -m blast_radius analyze \
  --repo /path/to/train-ticket \
  --change ts-order-service/src/.../OrderServiceImpl.java \
  | jq .
```

## Последствия

- Гексагональная структура позволяет переключить любой адаптер без изменения домена.
- Тесты домена не требуют tree-sitter / NetworkX — только чистый Python.
- При добавлении нового языка: 1 новый адаптер в `adapters/parsers/`, 0 изменений в домене.
- Local Mode работает без Docker, PostgreSQL, Redis — достаточно `pip install`.
- Переход на Production Mode: только замена адаптеров, домен не трогаем.
