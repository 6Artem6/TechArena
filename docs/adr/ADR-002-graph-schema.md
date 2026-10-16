# ADR-002: Схема графа (узлы, рёбра, идентификаторы)

**Статус:** Принято  
**Дата:** 2026-10-13

## Контекст

Граф — центральная структура данных движка. От схемы зависят: IoU-совместимость вывода, полнота обхода, расширяемость.

## Решение

### Тип графа

`networkx.MultiDiGraph` — направленный мультиграф (несколько рёбер разного типа между одной парой узлов).

### Типы узлов

| Тип | Пример ID | Описание |
|---|---|---|
| `SERVICE` | `ts-order-service` | Микросервис целиком |
| `LIBRARY` | `ts-common` | Разделяемая библиотека (не deployable; нет в docker-compose `services:`). Пример: `edu.fudan.common.*` (ADR-029) |
| `PACKAGE_ROLE` | `ts-order-service::controller` | Роль пакета (controller / service / repository / entity / dto / config / util) |
| `CLASS` | `ts-order-service::order.OrderServiceImpl` | Конкретный класс |
| `ABSTRACT_CLASS` | `ts-order-service::order.AbstractOrder` | Абстрактный класс (`abstract class`) |
| `INTERFACE` | `ts-order-service::order.OrderService` | Интерфейс (есть реализация `OrderServiceImpl`) |
| `REPOSITORY` | `ts-order-service::order.OrderRepository` | Spring Data репозиторий (`extends CrudRepository/JpaRepository/MongoRepository`). **Нет Impl-файла** — Spring генерирует прокси. Не искать реализацию (ADR-029) |
| `METHOD` | `ts-order-service::order.OrderServiceImpl#createOrder` | Метод (включая сигнатуру при перегрузке) |
| `FIELD` | `ts-order-service::order.OrderServiceImpl.orderRepository` | Поле класса |
| `DTO` | `ts-common::edu.fudan.common.entity.Order` | Общий контракт данных |
| `CONFIG_SECTION` | `docker-compose::ts-order-service.environment` | Секция конфига |
| `UnknownDynamicTarget` | `ts-order-service::dynamic#unknown_42` | Нераскрытый динамический вызов |

### Типы рёбер

| Ребро | Направление | Вес по умолчанию | Описание |
|---|---|---|---|
| `CONTAINS` | родитель → дочерний | — | Сервис→класс, класс→метод |
| `CALLS` | метод → метод | 1.0 | Вызов внутри сервиса |
| `CALLS_SERVICE` | сервис/метод → сервис | 1.0 | Межсервисный HTTP-вызов |
| `USES_DTO` | класс/метод → DTO | 0.3 | Использование общего контракта |
| `IMPLEMENTS` | класс → интерфейс | — | Реализация интерфейса |
| `IMPORTS` | файл → файл/модуль | — | Import-зависимость |
| `SHARES_RESOURCE` | сервис → сервис | 0.5 | Общая БД, очередь, том |
| `USES_MAGIC_CALL` | узел → UnknownDynamicTarget | 1.5 | Динамический / нераскрытый вызов |
| `ASYNC_MESSAGE` | сервис → сервис | 0.8 | Асинхронная очередь (RabbitMQ, Kafka); `metadata.queue_name` |
| `SHADOW_CALLS` | метод → метод | 0.4 | Закомментированный вызов метода (ADR-016) |
| `SHADOW_CALLS_SERVICE` | сервис → сервис | 0.4 | Закомментированный межсервисный вызов |
| `SHADOW_USES_DTO` | класс → DTO | 0.15 | Закомментированное использование DTO |
| `SHADOW_SHARES_RESOURCE` | сервис → ресурс | 0.2 | Закомментированная зависимость ресурса |

**Все SHADOW-рёбра** содержат в `metadata`: `{"shadow": true, "shadow_confidence": float, "comment_location": "file:line_start-line_end"}`.

### Формат идентификатора узла

```
{service_name}::{fqn_or_path}[#{member}]
```

Примеры:
- `ts-order-service::order.OrderServiceImpl`
- `ts-order-service::order.OrderServiceImpl#createOrder`
- `ts-common::edu.fudan.common.entity.Order`
- `docker-compose::ts-order-service.environment.SPRING_DATASOURCE_URL`

### Метаданные узла (атрибуты NetworkX)

```python
{
    "node_type": "CLASS",
    "service": "ts-order-service",
    "file_path": "ts-order-service/src/.../OrderServiceImpl.java",
    "annotations": ["@Service"],
    "arch_role": "business_logic",
    "arch_pattern": "MVC",
    "parse_status": "OK",     # OK | PARTIAL_PARSED
    "criticality": None,      # заполняется в фазе 4
    "summary": None,          # заполняется в фазе 3 (Ollama)
    # Метаданные из комментариев (ADR-019)
    "deprecated": False,              # True если @deprecated / // Deprecated
    "deprecated_since": None,         # "2.0" если найдено "since 2.0"
    "has_bug_note": False,            # True если FIXME/BUG/HACK в комментарии
    "bug_description": None,          # первые 80 символов FIXME-комментария
    # Поля ребра (не узла):
    # "shadow": False,             # True если ребро из закомментированного кода
    # "shadow_confidence": None,   # float 0..1 — уверенность классификатора
    # "comment_location": None,    # "file.java:5-12"
}
```

### Типы рёбер (дополнительные)

| Ребро | Атрибуты | Описание |
|---|---|---|
| `SIBLING_OF` | `split_condition`, `split_type`, `confidence` | Зеркальный сервис (order↔order-other). depth_cost=0 при обходе (ADR-027) |

## Обоснование

- `MultiDiGraph` нужен: между одной парой узлов могут быть и `CALLS`, и `USES_DTO`.
- Разделение `CLASS`, `INTERFACE`, `REPOSITORY`, `ABSTRACT_CLASS` критично: для `REPOSITORY` не искать `Impl`, для `INTERFACE` — искать.
- `LIBRARY` vs `SERVICE`: `ts-common` не разворачивается → не добавлять в граф как SERVICE.
- `PACKAGE_ROLE` как промежуточный узел позволяет абстрагироваться от конкретных классов при агрегации.
- Единый формат ID делает граф воспроизводимым между запусками.

## Последствия

- При изменении формата ID нужно обновлять JSON-контракт вывода.
- `PARTIAL_PARSED` узлы без дочерних рёбер могут занижать recall — отслеживать в статистике.

---

## Критический риск: совместимость NodeId с форматом оценки (IoU)

**Проблема:** Вся метрика IoU считается по пересечению множеств NodeId. Если наш формат не совпадает с форматом gold standard — IoU = 0 для всех узлов, даже при правильном графе.

**Неизвестные факторы:**
- Включают ли overloaded методы типы параметров: `#createOrder` или `#createOrder(OrderInfo)`?
- Формат FQN: с пакетом (`order.OrderServiceImpl`) или без (`OrderServiceImpl`)?
- Имя сервиса: `ts-order-service` или `ts-order` или полный Docker-путь?

**Стратегия смягчения:**

1. **Первый день доступа** — вывести 10-15 примерных NodeId, сверить с первым test case вручную.
2. **Хранить aliases** — несколько вариантов ID для одного узла:

```python
@dataclass
class GraphNode:
    primary_id: str          # основной формат: ts-order-service::order.OrderServiceImpl#createOrder
    aliases: list[str]       # запасные: без пакета, без параметров, short-service-name
    display_name: str

# Примеры aliases для одного узла:
aliases = [
    "ts-order::order.OrderServiceImpl#createOrder",       # short service name
    "ts-order-service::OrderServiceImpl#createOrder",     # без пакета
    "ts-order-service::order.OrderServiceImpl#createOrder(OrderInfo)",  # с параметрами
]
```

3. **Нормализатор на выходе** — если известен ожидаемый формат из условия хакатона, применять нормализацию перед выводом JSON:

```python
def normalize_node_id(node_id: str, target_format: str = "default") -> str:
    """Привести NodeId к нужному формату перед выводом."""
    if target_format == "no_package":
        # ts-order-service::order.OrderServiceImpl → ts-order-service::OrderServiceImpl
        parts = node_id.split("::")
        if len(parts) == 2:
            fqn = parts[1].rsplit(".", 1)[-1]  # взять только последнюю часть
            return f"{parts[0]}::{fqn}"
    return node_id
```

4. **Конфигурируемый формат** — параметр `NODE_ID_FORMAT` в `.env` или CLI-флаг. Не менять код — менять формат данных.
