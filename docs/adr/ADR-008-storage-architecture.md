# ADR-008: Архитектура хранилищ (PostgreSQL/pgvector, Redis, абстракции)

**Статус:** Принято — **Deferred (Post-Competition)**
> **Примечание:** Описывает PostgreSQL/pgvector + Redis. Для CLI-анализатора: NetworkX in-memory + pickle snapshot. Реализовать после стабильного IoU.  
**Дата:** 2026-10-13

## Контекст

Движок хранит граф зависимостей, векторные эмбеддинги и кэшированные результаты. Нужна асинхронная, масштабируемая, заменяемая стека хранилищ.

## Решение

### Стек хранилищ

| Хранилище | Версия / Библиотека | Назначение |
|---|---|---|
| PostgreSQL + pgvector | `asyncpg` 0.29+ | Узлы, рёбра, эмбеддинги, правила, few-shot банк, file hashes |
| Redis | `redis[asyncio]` 5.x | Очереди задач (arq), кэш blast radius, прогресс индексации |
| Pickle файл (shared volume) | stdlib | NetworkX snapshot для BFS в памяти (ADR-005) |

> **LanceDB исключён.** Все векторные операции — через pgvector в PostgreSQL.  
> Pickle — не хранилище данных, а транспорт графа между `worker-service` и `engine-api`.

### Управление соединениями — единое место

```
src/infrastructure/db/
├── connections.py    # Пулы соединений: create_db_pool(), create_redis_pool()
├── base.py           # Базовые утилиты: transaction(), retry()
└── migrations/       # SQL-миграции (alembic или plain .sql)
```

```python
# infrastructure/db/connections.py
import asyncpg
import redis.asyncio as aioredis
from typing import AsyncGenerator

_db_pool: asyncpg.Pool | None = None
_redis_pool: aioredis.Redis | None = None

async def create_db_pool(dsn: str) -> asyncpg.Pool:
    global _db_pool
    _db_pool = await asyncpg.create_pool(dsn, min_size=2, max_size=10)
    return _db_pool

async def create_redis_pool(url: str) -> aioredis.Redis:
    global _redis_pool
    _redis_pool = aioredis.from_url(url, decode_responses=True)
    return _redis_pool

# Геттеры — используются через DI (FastAPI Depends)
def get_db() -> asyncpg.Pool:
    assert _db_pool is not None, "DB pool not initialized"
    return _db_pool

def get_redis() -> aioredis.Redis:
    assert _redis_pool is not None, "Redis pool not initialized"
    return _redis_pool
```

Инициализация — один раз в `lifespan` (ADR-007). Использование — только через `get_db()` / `get_redis()`.

### Абстрактные интерфейсы репозиториев

```python
# domain/ports/graph_repo.py
from abc import ABC, abstractmethod
from domain.model.node import Node, NodeId
from domain.model.edge import Edge

class GraphRepository(ABC):
    @abstractmethod
    async def save_node(self, node: Node) -> None: ...

    @abstractmethod
    async def save_nodes_batch(self, nodes: list[Node]) -> None: ...

    @abstractmethod
    async def get_node(self, node_id: NodeId) -> Node | None: ...

    @abstractmethod
    async def find_neighbors(self, node_id: NodeId, edge_types: list[str] | None = None) -> list[Node]: ...

    @abstractmethod
    async def save_edge(self, edge: Edge) -> None: ...

    @abstractmethod
    async def save_edges_batch(self, edges: list[Edge]) -> None: ...

    @abstractmethod
    async def get_edges(self, node_id: NodeId) -> list[Edge]: ...


# domain/ports/vector_store.py
class VectorStore(ABC):
    @abstractmethod
    async def index_node(self, node_id: NodeId, vector: list[float], metadata: dict) -> None: ...

    @abstractmethod
    async def search_similar(self, vector: list[float], service: str | None = None,
                             node_type: str | None = None, top_k: int = 10) -> list[NodeId]: ...


# domain/ports/cache.py
class CachePort(ABC):
    @abstractmethod
    async def get(self, key: str) -> str | None: ...

    @abstractmethod
    async def set(self, key: str, value: str, ttl: int | None = None) -> None: ...

    @abstractmethod
    async def invalidate(self, pattern: str) -> None: ...
```

### Реализации (адаптеры)

```python
# adapters/graph/postgres_repo.py
class PostgresGraphRepository(GraphRepository):
    def __init__(self, pool: asyncpg.Pool): ...
    async def save_node(self, node: Node) -> None:
        await self._pool.execute(
            "INSERT INTO nodes(id, display_name, node_type, service, metadata) "
            "VALUES($1,$2,$3,$4,$5) ON CONFLICT(id) DO UPDATE SET ...",
            str(node.id), node.display_name, node.node_type, node.service, node.metadata_json
        )

# adapters/graph/networkx_repo.py  (для локальной отладки без БД)
class NetworkXGraphRepository(GraphRepository):
    def __init__(self): self._graph = nx.MultiDiGraph()
    ...

# adapters/cache/redis_cache.py
class RedisCacheAdapter(CachePort):
    def __init__(self, redis: aioredis.Redis): ...
    async def get(self, key: str) -> str | None:
        return await self._redis.get(key)
```

### Wrapper / DI-биндинг

```python
# infrastructure/container.py  — единое место регистрации зависимостей
from adapters.graph.postgres_repo import PostgresGraphRepository
from adapters.cache.redis_cache import RedisCacheAdapter
from domain.ports.graph_repo import GraphRepository

def make_graph_repository(pool: asyncpg.Pool) -> GraphRepository:
    return PostgresGraphRepository(pool)

def make_cache(redis: aioredis.Redis) -> CachePort:
    return RedisCacheAdapter(redis)
```

В `dependencies.py` (FastAPI):
```python
async def get_graph_repo(pool=Depends(get_db)) -> GraphRepository:
    return make_graph_repository(pool)
```

### PostgreSQL схема

```sql
-- Узлы графа
CREATE TABLE nodes (
    id          TEXT PRIMARY KEY,           -- NodeId строка
    display_name TEXT NOT NULL,
    node_type   TEXT NOT NULL,              -- SERVICE, CLASS, METHOD, ...
    service     TEXT NOT NULL,
    arch_role   TEXT,
    file_path   TEXT,
    metadata    JSONB,
    embedding   vector(768),               -- pgvector: GraphCodeBERT dim
    created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX ON nodes USING ivfflat (embedding vector_cosine_ops);
CREATE INDEX ON nodes (service);
CREATE INDEX ON nodes (node_type);

-- Рёбра графа
CREATE TABLE edges (
    id          BIGSERIAL PRIMARY KEY,
    source_id   TEXT REFERENCES nodes(id),
    target_id   TEXT REFERENCES nodes(id),
    edge_type   TEXT NOT NULL,
    weight      FLOAT DEFAULT 1.0,
    metadata    JSONB
);
CREATE INDEX ON edges (source_id);
CREATE INDEX ON edges (target_id);
CREATE INDEX ON edges (edge_type);
```

### Redis — очереди и кэш

```
blast_radius:{hash(change_spec)}   → JSON результат (TTL: сессия анализа)
worker:queue:parse                 → List<task_json> (LPUSH/BRPOP)
worker:queue:embed                 → List<task_json>
worker:queue:uncertain             → List<item_json> (low-confidence, ADR-014)
index:status:{task_id}             → {status, progress, errors}
graph:snapshot:version             → {version, created_at, node_count}
```

Библиотека для воркер-очереди: `arq` (async Redis Queue) — нативная asyncio, совместима с FastAPI.

### Инкрементальная индексация (file hash)

```sql
-- Таблица отслеживания файлов
CREATE TABLE indexed_files (
    file_path   TEXT PRIMARY KEY,
    file_hash   TEXT NOT NULL,       -- SHA-256 содержимого файла
    indexed_at  TIMESTAMPTZ DEFAULT now(),
    node_count  INT,
    status      TEXT                 -- OK | PARTIAL_PARSED | ERROR
);
```

```python
def should_reindex(file_path: str, current_hash: str, db_hash: str | None) -> bool:
    """Переиндексируем только если файл изменился или ещё не индексирован."""
    return db_hash is None or db_hash != current_hash
```

При повторной индексации:
1. Для каждого файла вычислить SHA-256.
2. Сравнить с `indexed_files.file_hash` И `indexed_files.rules_version` с `rules:current_version` из Redis.
3. Если хэш совпадает И версия правил актуальна → пропустить.
4. Если файл изменился → полный переиндекс файла.
5. Если появились новые правила → проверить `rule.affects_files` glob → только затронутые файлы.
6. Обновить `indexed_files` (hash + rules_version).

```sql
-- Добавляем в indexed_files версию правил
ALTER TABLE indexed_files ADD COLUMN IF NOT EXISTS rules_version INT DEFAULT 0;
```

**Rule-triggered reindex** (ADR-018): новое правило → `rules:current_version` инкрементируется в Redis → следующая индексация найдёт расхождение версий → только файлы из `rule.affects_files` переиндексируются без изменения хэша.

### Порядок старта сервисов

```yaml
# docker-compose.yml (фрагмент)
services:
  postgres:
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U blast"]
      interval: 5s

  ml-service:
    depends_on:
      postgres:
        condition: service_healthy  # не нужен для ml-service, но для порядка

  worker-service:
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
      ml-service:
        condition: service_healthy

  engine-api:
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    # engine-api НЕ зависит от worker-service:
    # он читает из БД/pickle, а не взаимодействует с воркером напрямую
```

ETL seed (запускается вручную после первого старта):
```bash
make db-seed  # PostgreSQL + ml-service должны быть ready
```

## Обоснование

- **asyncpg** быстрее `aiopg` и `psycopg3[binary]` для высокопараллельных сценариев.
- **pgvector** — единственное векторное хранилище: граф + эмбеддинги в одном месте. LanceDB исключён.
- **File hash**: повторная индексация при ~5 изменённых файлах ≈ секунды вместо минут.
- **Единый `connections.py`**: нет разрозненных подключений по проекту.
- **arq**: нативный asyncio, не требует отдельного Celery-воркера.

## Последствия

- Локальная разработка без PostgreSQL: использовать `NetworkXGraphRepository` (флаг `--storage=memory`).
- pgvector требует PostgreSQL 15+ и `CREATE EXTENSION vector`.
- `ml-service` должен быть ready перед `make db-seed` (нужны эмбеддинги для seed-примеров).
- Shared volume `/data/` между `worker-service` и `engine-api` для pickle-снапшота (ADR-005).
