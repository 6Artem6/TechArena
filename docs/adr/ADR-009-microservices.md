# ADR-009: Декомпозиция на микросервисы и транспорт

**Статус:** Принято — **Deferred (Post-Competition)**
> **Примечание:** Описывает engine-api + worker-service + ml-service. Для хакатона: однопроцессный CLI. Реализовать после стабильного IoU.  
**Дата:** 2026-10-13

## Контекст

Движок состоит из нескольких логически независимых компонентов с разными профилями нагрузки: CPU-heavy парсинг, I/O-heavy API, ML-инференс, UI. Нужно разделение, которое не создаёт избыточной сложности.

## Решение

### Состав микросервисов (MVP)

```mermaid
graph TD
    UI[ui-service\nReact + nginx] -->|REST| API[engine-api\nFastAPI]
    API -->|arq queue| Redis[(Redis)]
    API -->|read graph| PG[(PostgreSQL\n+pgvector)]

    Redis -->|tasks| Worker[worker-service\nparse workers]
    Worker -->|write nodes/edges| PG
    Worker -->|write embeddings| PG
    Worker -->|progress/cache| Redis
    Worker -->|REST: embed/summarize| ML[ml-service\nstateless inference]

    ML -.->|no DB access| X[ ]
    style X display:none
```

> **Важно:** `worker-service` — единственный, кто пишет в PostgreSQL.  
> `ml-service` — stateless: принимает текст/код, возвращает векторы/саммари, не хранит ничего.

### Описание сервисов

#### `engine-api` (FastAPI)
- Принимает запросы от UI и CLI
- Оркестрирует use cases: `AnalyzeBlastRadius`, `BuildGraph`
- Кладёт задачи индексации в Redis-очередь (arq)
- Читает граф из PostgreSQL для ответа на запросы
- **Профиль:** I/O bound, много параллельных HTTP-запросов
- **Порт:** 8000

#### `worker-service`
- Слушает очередь Redis (arq worker)
- Параллельный парсинг файлов репозитория (`ProcessPoolExecutor` внутри)
- **Единственный сервис, пишущий в PostgreSQL**: узлы, рёбра, эмбеддинги, метаданные
- Вызывает `ml-service` для получения эмбеддингов/саммари, сам записывает результат в БД
- Обновляет прогресс задачи в Redis
- **Профиль:** CPU-heavy (tree-sitter парсинг), I/O (запись в БД)
- Масштабируется горизонтально: несколько реплик читают из одной Redis-очереди

#### `ml-service` (stateless)
- **Только инференс, никакого хранилища**: принимает текст/код, возвращает результат
- Генерация эмбеддингов (GraphCodeBERT / BGE-M3): `POST /embed/batch`
- Ollama-инференс для саммари и domain_role: `POST /summarize/batch`
- Все ML-модели загружаются при старте и живут в памяти
- **Нет доступа к PostgreSQL или Redis**
- **Профиль:** CPU/GPU bound, батчевые запросы
- **Порт:** 8001

#### `ui-service`
- React SPA (Vite + TypeScript)
- Nginx раздаёт статику, проксирует `/api` на `engine-api`
- **Порт:** 3000

### Docker Compose (локальная среда)

```yaml
services:
  engine-api:
    build: ./services/engine-api
    ports: ["8000:8000"]
    depends_on: [postgres, redis]
    environment:
      DATABASE_URL: postgresql://...
      REDIS_URL: redis://redis:6379

  worker-service:
    build: ./services/worker-service
    depends_on: [postgres, redis, ml-service]
    environment:
      DATABASE_URL: postgresql://...
      REDIS_URL: redis://redis:6379
      ML_SERVICE_URL: http://ml-service:8001
    volumes:
      - ./train-ticket:/data/repos/train-ticket:ro  # репо только для чтения

  ml-service:
    build: ./services/ml-service
    ports: ["8001:8001"]
    volumes:
      - ./models:/models  # локальные ML-модели

  ui-service:
    build: ./services/ui-service
    ports: ["3000:3000"]

  postgres:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_DB: blast_radius
      POSTGRES_USER: blast
      POSTGRES_PASSWORD: blast

  redis:
    image: redis:7-alpine
    command: redis-server --maxmemory 512mb --maxmemory-policy allkeys-lru
```

### Транспорт между сервисами

**Сейчас: REST (HTTP/JSON)**
- Простота реализации
- Стандартный HTTP клиент: `httpx` (async) везде
- Таймауты и retry через `httpx` middleware

**В будущем: gRPC**
- Переход оправдан при:
  - Частых вызовах `ml-service` с большими batch-эмбеддингами (бинарный протокол быстрее)
  - Стриминге результатов индексации
- Переход не ломает логику: только новый адаптер транспорта, use cases не меняются
- Protobuf-схемы заготовить заранее (`services/proto/`)

### Параллельность при обходе репозитория

```
worker-service
    │
    ├── arq job: index_repository(repo_path, task_id)
    │       │
    │       └── BuildGraphUseCase
    │               │
    │               ├── FileWalker → [47 сервисов]
    │               │
    │               └── ProcessPoolExecutor (N воркеров)
    │                     ├── Worker: parse ts-order-service
    │                     ├── Worker: parse ts-preserve-service
    │                     └── ...
    │                     └── Aggregator → batch INSERT → PostgreSQL
    │
    └── Shared state:
          - PostgreSQL: финальный граф
          - Redis: прогресс задачи (index:status:{task_id})
```

Несколько параллельных запросов на индексацию одного репо:
- Первый запрос создаёт задачу, остальные получают тот же `task_id` (дедупликация по hash(repo_path)).
- Пока задача выполняется — возвращаем `status: in_progress`.

## Обоснование

- Разделение `engine-api` и `worker-service`: API не блокируется CPU-heavy парсингом.
- `ml-service` отдельно: модели (GraphCodeBERT, Ollama) тяжёлые, загружаются один раз при старте; другие сервисы не тащат ML-зависимости.
- REST сейчас: минимальная инфраструктура, gRPC — управляемое улучшение позже.

## Последствия

- Для хакатона: можно запустить всё в одном процессе (флаг `--mode=monolith`), контейнеры — для финального деплоя.
- gRPC-переход: добавить `services/proto/`, новые адаптеры; бизнес-логика не меняется.
- Горизонтальное масштабирование `worker-service`: несколько реплик + одна Redis-очередь.
