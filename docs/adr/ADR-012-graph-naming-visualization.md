# ADR-012: Именование узлов графа и визуализация

**Статус:** Принято — **Deferred (Post-Competition)**
> **Примечание:** Описывает PyVis / Vis.js визуализацию графа. Не нужно для IoU. Реализовать после стабильного IoU.  
**Дата:** 2026-10-13

## Контекст

При 47 сервисах и сотнях классов граф быстро становится нечитаемым. Нужна система компактных имён для отображения и интерактивный UI для навигации без потери деталей.

## Решение

### Двухуровневое именование

Каждый узел имеет два имени:

| Поле | Пример | Использование |
|---|---|---|
| `id` (NodeId) | `ts-order-service::order.OrderServiceImpl#createOrder` | Внутренние ссылки, IoU, БД |
| `display_name` | `order / Impl → createOrder` | Граф UI, CLI вывод |

### Правила формирования `display_name`

```python
def make_display_name(node: Node) -> str:
    match node.node_type:
        case "SERVICE":
            # ts-order-service → order
            return node.service.removeprefix("ts-").removesuffix("-service")

        case "CLASS":
            # order.OrderServiceImpl → Impl  (суффикс, если содержит "Impl")
            # order.OrderController → Controller
            short = node.fqn.split(".")[-1]
            return shorten_class_name(short)  # убирает повторяющийся префикс сервиса

        case "INTERFACE":
            short = node.fqn.split(".")[-1]
            return f"«{short}»"  # интерфейсы в кавычках

        case "METHOD":
            return node.member  # только имя метода

        case "DTO":
            return node.fqn.split(".")[-1]  # Order, User, Route

        case "CONFIG_SECTION":
            return node.section_key  # environment.DATASOURCE_URL

        case "UnknownDynamicTarget":
            return "?"

def shorten_class_name(name: str) -> str:
    # OrderServiceImpl → Impl (если суффикс стандартный)
    for suffix in ["ServiceImpl", "RepositoryImpl", "Controller", "Service",
                   "Repository", "Entity", "Config", "Handler", "Listener"]:
        if name.endswith(suffix):
            return suffix
    return name  # без изменений
```

### Визуальные уровни (LOD — Level of Detail)

Граф рендерится в трёх уровнях детализации:

```
LOD 0 (zoom out):    [order] ──► [station] ──► [preserve]
                      SERVICE       SERVICE       SERVICE

LOD 1 (zoom mid):   [order]
                      ├── Controller
                      ├── «OrderService»
                      └── Impl
                          └── ──► [station]

LOD 2 (zoom in):    [Impl]
                      ├── createOrder ──► [station::Controller#queryForNameList]
                      ├── getSoldTickets
                      └── ...
```

Переход между LOD — по зуму (автоматически) или по клику на узел (разворачивание).

### Цветовая схема узлов

| Тип узла | Цвет | Иконка |
|---|---|---|
| `SERVICE` | Синий | □ |
| `CLASS` | Зелёный | ○ |
| `INTERFACE` | Фиолетовый | ◇ |
| `METHOD` | Серый | · |
| `DTO` | Оранжевый | △ |
| `CONFIG_SECTION` | Жёлтый | ⚙ |
| `UnknownDynamicTarget` | Красный | ? |

Критичность (фаза 4): размер узла пропорционален `Score`.

### Цветовая схема рёбер

| Тип ребра | Цвет | Стиль |
|---|---|---|
| `CONTAINS` | Серый | пунктир |
| `CALLS` | Зелёный | сплошная |
| `CALLS_SERVICE` | Синий | жирная |
| `USES_DTO` | Оранжевый | тонкая |
| `IMPLEMENTS` | Фиолетовый | штрих |
| `SHARES_RESOURCE` | Жёлтый | двойная |
| `USES_MAGIC_CALL` | Красный | волнистая |

### Интерактивность (React UI)

**Панель управления:**
- Фильтр по сервису (мультиселект)
- Фильтр по типу узла (чекбоксы)
- Фильтр по критичности (ползунок)
- Фильтр по типу ребра (чекбоксы)
- Поиск по имени узла (автодополнение)

**Взаимодействие с узлом:**
- Hover → тултип: `display_name`, тип, сервис
- Клик → боковая панель: полный FQN, аннотации, файл, метрики, список рёбер
- Двойной клик → разворачивание/сворачивание дочерних узлов (LOD)
- Правая кнопка → контекстное меню: "Показать blast radius", "Изолировать", "Свернуть"

**Blast radius overlay:**
- После запроса анализа — затронутые узлы подсвечиваются (жёлтый/красный по критичности)
- Незатронутые — затемняются
- Путь распространения — анимированные рёбра

**Библиотека:** `sigma.js` v3 + `graphology`
- WebGL-рендеринг: 5000+ узлов без фризов
- `graphology` — граф как структура данных, sigma — только рендеринг
- Кастомные шейдеры для рёбер разного типа

### CLI вывод (compact mode)

```bash
$ python -m cli analyze --file ts-order-service/.../OrderServiceImpl.java --method createOrder

Blast Radius для order/Impl → createOrder (6 узлов):

SERVICE   order       ts-order-service
CLASS     Impl        ts-order-service::order.OrderServiceImpl
METHOD    createOrder ts-order-service::order.OrderServiceImpl#createOrder
SERVICE   station     ts-station-service
CLASS     Controller  ts-station-service::stationinfo.StationInfoController
METHOD    queryForNameList  ts-station-service::stationinfo.StationInfoController#queryForNameList

JSON → blast_radius_output.json
```

## Обоснование

- Компактные имена: `order` вместо `ts-order-service`, `Impl` вместо `OrderServiceImpl` — граф читается за секунды.
- LOD: не перегружать граф при высоком zoom out, детали — при необходимости.
- Sigma.js: единственная WebGL-библиотека с хорошим React integration и поддержкой 5k+ узлов.
- Двухуровневое именование: `display_name` меняется (для UX), `id` стабилен (для IoU, БД, API).

## Последствия

- `display_name` не уникален (два сервиса могут иметь `Impl`-класс). Уникальность — только в `id`.
- При LOD нужен efficient subgraph extraction из PostgreSQL (индексы на `service` + `node_type`).
- Логика `shorten_class_name` расширяема: добавить паттерны без изменения схемы.
