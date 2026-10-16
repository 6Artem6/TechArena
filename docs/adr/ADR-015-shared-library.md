# ADR-015: Общая библиотека blast-radius-core

**Статус:** Принято — **Deferred (Post-Competition)**
> **Примечание:** Описывает общую Maven-библиотеку blast-radius-core. Преждевременная абстракция для однопроцессного CLI. Реализовать после стабильного IoU.  
**Дата:** 2026-10-13  
**Связанные:** [ADR-006](ADR-006-engine-architecture.md), [ADR-009](ADR-009-microservices.md)

## Контекст

`engine-api` и `worker-service` работают с одними и теми же доменными объектами: `Node`, `Edge`, `DependencyGraph`, `ChangeSpec`, `BlastRadius`. Дублирование кода между сервисами → рассинхронизация моделей, сложность поддержки.

## Решение

Вынести общий домен в отдельный Python-пакет **`blast-radius-core`**, который оба сервиса устанавливают как локальную зависимость.

### Структура пакета

```
blast-radius-core/
├── pyproject.toml                # name = "blast-radius-core", version = "0.1.0"
├── README.md
└── src/
    └── blast_radius_core/
        ├── __init__.py
        ├── model/
        │   ├── __init__.py
        │   ├── node.py           # NodeId, Node, NodeType
        │   ├── edge.py           # Edge, EdgeType, EdgeWeight
        │   ├── graph.py          # DependencyGraph (aggregate root)
        │   └── change_spec.py    # ChangeSpec, BlastRadius (value objects)
        ├── services/
        │   ├── __init__.py
        │   ├── blast_radius.py   # BlastRadiusCalculator
        │   └── criticality.py    # NodeCriticality (фаза 4)
        └── ports/
            ├── __init__.py
            ├── graph_repo.py     # GraphRepository (ABC)
            ├── vector_store.py   # VectorStore (ABC)
            └── cache.py          # CachePort (ABC)
```

### Подключение в сервисах

```toml
# services/engine-api/pyproject.toml
[tool.poetry.dependencies]
blast-radius-core = { path = "../../blast-radius-core", develop = true }

# services/worker-service/pyproject.toml
[tool.poetry.dependencies]
blast-radius-core = { path = "../../blast-radius-core", develop = true }
```

В Docker:

```dockerfile
# services/engine-api/Dockerfile
COPY blast-radius-core/ /blast-radius-core/
RUN pip install -e /blast-radius-core
COPY services/engine-api/ .
RUN pip install -e .
```

### Структура репозитория с учётом пакета

```
blast-radius-engine/
├── blast-radius-core/       # ← общий пакет (нет внешних зависимостей)
│   └── src/blast_radius_core/
├── services/
│   ├── engine-api/
│   ├── worker-service/
│   ├── ml-service/
│   └── ui-service/
├── docker-compose.yml
└── Makefile
```

### Makefile — цели для пакета

```makefile
core-install:   ## Установить blast-radius-core в режиме разработки
    pip install -e blast-radius-core

core-test:      ## Тесты доменной логики (без внешних зависимостей)
    pytest blast-radius-core/tests/
```

## Обоснование

- Домен без внешних зависимостей → тесты быстрые, изолированные.
- Одно определение `NodeId` и `EdgeType` → нет рассинхронизации между сервисами.
- `develop = true` → изменение в `blast-radius-core` сразу видно в обоих сервисах без переустановки.

## Последствия

- `ml-service` **не** использует `blast-radius-core` — он stateless и работает только с текстом/векторами.
- При изменении доменной модели (например, новый `NodeType`) — одно место правки, оба сервиса обновляются автоматически.
- Версия пакета фиксируется в `pyproject.toml` каждого сервиса для воспроизводимости деплоя.
