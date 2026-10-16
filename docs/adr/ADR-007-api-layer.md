# ADR-007: API-слой (FastAPI, роуты, Pydantic v2, CLI)

**Статус:** Принято — **Deferred (Post-Competition)**
> **Примечание:** Описывает production REST API (FastAPI, Pydantic v2, Swagger). Для CLI-режима достаточно `argparse`/`typer` + JSON stdout. Реализовать после стабильного IoU.  
**Дата:** 2026-10-13

## Контекст

Движок должен быть доступен снаружи (REST API для UI и тестирования) и через CLI (отладка по файлу или вставке кода). Структура API должна быть предсказуемой, расширяемой и не смешивать транспорт с логикой.

## Решение

### Структура API-сервиса (`engine-api`)

```
src/
└── api/
    ├── main.py               # Точка входа: создание app, регистрация роутеров, middleware, lifespan
    ├── dependencies.py       # DI-геттеры: get_db(), get_redis(), get_graph_service()
    ├── routers/
    │   ├── graph.py          # GET /graph/nodes, GET /graph/edges, GET /graph/node/{id}
    │   ├── analyze.py        # POST /analyze/blast-radius
    │   ├── index.py          # POST /index/repository (запуск индексации)
    │   └── health.py         # GET /health, GET /health/ready
    ├── dto/
    │   ├── requests.py       # Pydantic v2: ChangeSpecRequest, IndexRequest
    │   └── responses.py      # Pydantic v2: BlastRadiusResponse, NodeResponse, GraphStatsResponse
    └── middleware/
        └── logging.py        # Request ID, structured logging (все логи → stderr/файл, не stdout)
```

### Правила организации файлов

| Файл | Что содержит | Что НЕ содержит |
|---|---|---|
| `main.py` | `FastAPI()`, подключение роутеров, lifespan, CORS | бизнес-логику, SQL-запросы |
| `dependencies.py` | `get_db()`, `get_redis()`, `get_graph_service()` | логику сервисов |
| `routers/*.py` | HTTP-хендлеры, валидация входа/выхода | прямые обращения к БД |
| `dto/*.py` | Pydantic v2 модели запросов и ответов | бизнес-объекты домена |
| `domain/services/*.py` | бизнес-логика | HTTP-детали, Pydantic |

### Pydantic v2 — DTO-контракты

```python
# dto/requests.py
from pydantic import BaseModel, Field

class ChangeSpecRequest(BaseModel):
    file: str = Field(..., description="Путь к файлу относительно корня репо")
    class_name: str | None = None
    method: str | None = None
    config_section: str | None = None
    max_depth: int = Field(default=6, ge=1, le=20)

class IndexRequest(BaseModel):
    repo_path: str
    languages: list[str] = ["java", "python", "javascript"]
    parallel_workers: int = Field(default=4, ge=1, le=16)


# dto/responses.py
class NodeResponse(BaseModel):
    id: str
    display_name: str
    node_type: str
    service: str
    arch_role: str | None
    criticality: float | None

class BlastRadiusResponse(BaseModel):
    change_spec: ChangeSpecRequest
    affected_nodes: list[NodeResponse]
    total_count: int
    graph_stats: dict
```

### Маршруты API

```
POST /api/v1/index/repository          # Запуск индексации репо (async задача)
GET  /api/v1/index/status/{task_id}    # Статус задачи индексации

POST /api/v1/analyze/blast-radius      # Расчёт blast radius → JSON (один change_spec)
POST /api/v1/analyze/batch             # Параллельный расчёт списка change_spec (N штук)
GET  /api/v1/graph/nodes               # Список узлов (с пагинацией, фильтрами)
GET  /api/v1/graph/node/{node_id}      # Детали узла
GET  /api/v1/graph/edges               # Список рёбер
GET  /api/v1/graph/service/{name}      # Все узлы сервиса
GET  /api/v1/health                    # Liveness
GET  /api/v1/health/ready              # Readiness (проверка БД, Redis)
```

### Batch endpoint — параллельный расчёт (для evaluation)

Принимает список `change_spec`, запускает BFS для каждого в `ProcessPoolExecutor` через `asyncio.gather`:

```python
# routers/analyze.py
@router.post("/analyze/batch")
async def analyze_batch(
    specs: list[ChangeSpecRequest],
    executor: ProcessPoolExecutor = Depends(get_executor),
) -> list[BlastRadiusResponse]:
    """Параллельный расчёт blast radius для списка изменений.

    Каждый BFS независим — работают в разных процессах через fork().
    Граф разделяется через CoW — нет накладных расходов на копирование.
    """
    loop = asyncio.get_event_loop()
    # ProcessPoolExecutor в dependencies.py: размер = cpu_count()
    futures = [
        loop.run_in_executor(executor, _bfs_worker, spec.model_dump())
        for spec in specs
    ]
    return await asyncio.gather(*futures)

# Размер пула: cpu_count() — оптимально для CPU-bound BFS
# dependencies.py: ProcessPoolExecutor(max_workers=os.cpu_count())
```

```python
# dto/requests.py — расширение
class BatchAnalyzeRequest(BaseModel):
    specs: list[ChangeSpecRequest]
    max_parallel: int = Field(default=8, ge=1, le=32)
    include_shadow: bool = True
```

### CLI (`infrastructure/cli.py`)

Инструмент для локальной отладки без поднятия HTTP-сервера. Использует те же use cases.

```bash
# Анализ по файлу
python -m cli analyze --file ts-order-service/src/.../OrderServiceImpl.java \
                      --class order.OrderServiceImpl \
                      --method createOrder

# Анализ из stdin (вставка пути)
echo "ts-order-service/src/.../OrderServiceImpl.java" | python -m cli analyze

# Построение индекса
python -m cli index --repo ./train-ticket --workers 8

# Просмотр узла
python -m cli node ts-order-service::order.OrderServiceImpl
```

Вывод CLI: чистый JSON в stdout (тот же контракт, что и API).

### lifespan и соединения

```python
# main.py
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    await db_pool.connect()      # инициализация пула (см. ADR-008)
    await redis_pool.connect()
    yield
    await db_pool.disconnect()
    await redis_pool.disconnect()

app = FastAPI(lifespan=lifespan)
```

## Обоснование

- **FastAPI**: нативная async поддержка, автогенерация OpenAPI, Pydantic v2 встроен.
- **Разделение роутеров**: каждый роутер — отдельная область ответственности; легко добавить новый без изменения остальных.
- **CLI через те же use cases**: один код пути, два транспорта.
- **Pydantic v2**: `model_validate`, `model_dump(mode='json')` — строгая типизация без оверхеда.

## Последствия

- `dto/` модели — это граница API, не доменные объекты. Конвертация в domain-объекты — в use case, не в роутере.
- Добавление gRPC (ADR-009) потребует только нового слоя транспорта, use cases не меняются.
