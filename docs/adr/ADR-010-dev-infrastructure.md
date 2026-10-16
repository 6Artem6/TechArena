# ADR-010: Dev-инфраструктура (Makefile, Docker, React UI)

**Статус:** Принято — **Deferred (Post-Competition)**
> **Примечание:** Описывает Makefile + Docker Compose + React UI (sigma.js). Для хакатона: минимальный Makefile без UI. Реализовать после стабильного IoU.  
**Дата:** 2026-10-13

## Контекст

Разработка ведётся локально. Нужна единая точка входа для всех операций: запуск, сборка, тесты, линтинг, Docker.

## Решение

### Makefile — полный список целей

```makefile
# ── Python / Backend ──────────────────────────────────────
install:          ## Установить backend dev-зависимости
format:           ## isort + black
lint:             ## mypy
check:            ## Проверка без изменений (CI)
fix:              ## format + lint
test:             ## pytest с покрытием
test-unit:        ## Только unit-тесты (без БД)
test-integration: ## Тесты с реальной БД (требует docker-up)

# ── Frontend ───────────────────────────────────────────────
ui-install:       ## npm install в services/ui-service
ui-dev:           ## Vite dev сервер (hot reload)
ui-build:         ## Vite production build → services/ui-service/dist
ui-lint:          ## eslint + tsc --noEmit

# ── Docker ────────────────────────────────────────────────
docker-build:     ## docker compose build --no-cache
docker-up:        ## docker compose up -d
docker-down:      ## docker compose down
docker-logs:      ## docker compose logs -f
docker-ps:        ## docker compose ps

# ── DB ────────────────────────────────────────────────────
db-migrate:       ## Применить SQL миграции
db-reset:         ## Дроп + пересоздание схемы (только dev)

# ── Dev shortcuts ──────────────────────────────────────────
dev:              ## docker-up + запуск engine-api локально (без контейнера)
index:            ## Запустить индексацию train-ticket
analyze:          ## Пример: make analyze FILE=ts-order-service/...

install-hooks:    ## Установить git pre-commit хук
```

### React UI (`services/ui-service`)

```
services/ui-service/
├── package.json
├── vite.config.ts
├── tsconfig.json
├── src/
│   ├── main.tsx
│   ├── App.tsx
│   ├── api/
│   │   ├── client.ts         # axios / fetch обёртка с base URL
│   │   ├── graph.ts          # GET /graph/nodes, /graph/node/:id
│   │   └── analyze.ts        # POST /analyze/blast-radius
│   ├── components/
│   │   ├── GraphCanvas.tsx   # Интерактивный граф (sigma.js или react-force-graph)
│   │   ├── NodeDetail.tsx    # Детали выбранного узла
│   │   ├── AnalyzeForm.tsx   # Форма change_spec
│   │   └── ResultPanel.tsx   # Список затронутых узлов
│   └── store/
│       └── graphStore.ts     # Zustand: текущий граф, выделенные узлы
```

**Библиотека графа:** `sigma.js` (v3) + `graphology` — быстрая, WebGL-рендеринг, хорошо масштабируется на 1000+ узлов. Альтернатива: `react-force-graph` (легче, D3-based).

**Взаимодействие с графом:**
- Компактные имена узлов по умолчанию (ADR-012)
- Клик по узлу → панель с полным FQN, аннотациями, критичностью
- Фильтры: по сервису, по типу узла, по уровню критичности
- Выделение blast radius: затронутые узлы подсвечиваются

### Nginx конфигурация (ui-service)

```nginx
location /api/ {
    proxy_pass http://engine-api:8000/api/;
}
location / {
    root /usr/share/nginx/html;
    try_files $uri $uri/ /index.html;  # SPA fallback
}
```

### Docker multi-stage builds

```dockerfile
# services/engine-api/Dockerfile
FROM python:3.11-slim AS base
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

FROM base AS dev
RUN pip install -r requirements-dev.txt
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--reload"]

FROM base AS prod
COPY src/ ./src/
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--workers", "4"]
```

## Обоснование

- Единый Makefile: не нужно помнить длинные команды.
- Sigma.js: WebGL-рендеринг держит 3000+ узлов без фризов, в отличие от SVG-based библиотек.
- Multi-stage Docker: dev-образ с hot reload, prod-образ без лишних зависимостей.

## Последствия

- `ui-build` и `docker-build` — в Makefile, CI запускает те же команды.
- `make dev`: PostgreSQL и Redis в контейнерах, Python API — локально для быстрого итерирования.
