# ADR-004: Уровни абстракции узлов и архитектурный слой

**Статус:** Принято  
**Дата:** 2026-10-13

## Контекст

Граф должен отражать семантические уровни кода, а не только файловую структуру. Это нужно для корректного распространения изменений и для понимания "веса" затронутого узла.

## Решение

### Иерархия уровней (сверху вниз)

```
ARCH_PATTERN          ← MVC / Hexagonal / Clean Architecture / CQRS / EDA / FLAT
    └── SERVICE       ← ts-order-service
          └── PACKAGE_ROLE   ← controller / service / repository / entity / dto / util / config
                └── CLASS / INTERFACE
                      └── METHOD / FIELD
```

### Определение архитектурного паттерна

Сигналы (применяются в порядке убывания приоритета):

| Паттерн | Сигнал из директорий | Сигнал из AST |
|---|---|---|
| **Clean Architecture** | пакеты `domain`, `application`, `infrastructure`, `interfaces` | UseCase-классы, Port-интерфейсы |
| **Hexagonal** | пакеты `ports`, `adapters`, `domain` | интерфейсы в `ports`, классы в `adapters` |
| **CQRS** | пакеты `command`, `query`, `handler` | классы с суффиксом `Command`, `Query`, `Handler` |
| **EDA** | `event`, `listener`, `publisher` | `@EventListener`, `@KafkaListener`, `@RabbitListener` |
| **MVC** | `controller`, `service`, `repository` | `@Controller`/`@RestController`, `@Service`, `@Repository` |
| **FLAT** | — | всё остальное |

Если сервис не попадает ни в один паттерн → `FLAT`. Это не ошибка, просто метаданные.

### Определение роли пакета (PACKAGE_ROLE)

Словарь ключевых слов для имён директорий:

```python
ROLE_MAP = {
    "controller": "http_handler",
    "controllers": "http_handler",
    "rest": "http_handler",
    "web": "http_handler",
    "service": "business_logic",
    "services": "business_logic",
    "usecase": "business_logic",
    "repository": "data_access",
    "repositories": "data_access",
    "dao": "data_access",
    "persistence": "data_access",
    "entity": "data_model",
    "entities": "data_model",
    "model": "data_model",
    "models": "data_model",
    "dto": "contract",
    "dtos": "contract",
    "request": "contract",
    "response": "contract",
    "config": "configuration",
    "configuration": "configuration",
    "util": "utility",
    "utils": "utility",
    "helper": "utility",
    "exception": "error_handling",
    "error": "error_handling",
    "security": "security",
    "auth": "security",
    "event": "event",
    "listener": "event",
    "publisher": "event",
    "port": "port",
    "adapter": "adapter",
    "domain": "domain",
    "application": "application_layer",
    "infrastructure": "infrastructure",
}
```

Если директория не в словаре → роль `UNKNOWN`, класс всё равно добавляется в граф.

**Важно:** `ROLE_MAP` — bootstrap-источник для `make db-seed`. При прогреве каждая запись загружается в unified `rules` table (ADR-018) с `scope=package_name, pattern_type=string_match, source=builtin`. В runtime путь code не обращается к Python-словарю — только к `rules` table через `RuleEngine`.

**Fuzzy для UNKNOWN**: если имя директории не найдено точно → `RuleEngine.match(scope="package_name", text=dir_name)` через BGE-M3. Например, `impl` → fuzzy match к `service` (similarity=0.82) → роль `business_logic`.

## Обоснование

- Разделение `CLASS` и `INTERFACE` (ADR-002) работает только при явном `IMPLEMENTS` ребре, которое строится на этом же уровне.
- `PACKAGE_ROLE` как промежуточный узел позволяет задавать вопрос: "все ли контроллеры сервиса затронуты?" без перебора классов.
- Архитектурный паттерн — это метаданные SERVICE-узла, не отдельный уровень в иерархии.

## Влияние комментариев на criticality (ADR-019)

Метаданные из комментариев (`deprecated`, `has_bug_note`) применяются мультипликативно к базовому весу критичности:

```python
def apply_comment_metadata(criticality: float, node_attrs: dict) -> float:
    """Корректировка criticality на основе comment metadata.

    deprecated=True → код устаревший, изменения менее критичны.
    has_bug_note=True → известный баг, изменения несут больший риск.
    """
    if node_attrs.get("deprecated"):
        criticality *= 0.5
    if node_attrs.get("has_bug_note"):
        criticality *= 1.3
    return criticality
```

В JSON-выводе `deprecated: true` и `has_bug_note: true` включаются как предупреждения — помогают интерпретировать blast radius.

## Последствия

- Проекты без стандартных имён директорий (например, пакет `impl`) получат роль `UNKNOWN` — это допустимо.
- `train-ticket` преимущественно MVC с flat-структурой — большинство сервисов получат `MVC` или `FLAT`.
- Архитектурный слой не влияет на BFS-обход напрямую, но используется в фазе 4 для весов критичности.
- `deprecated` узлы остаются в blast radius — изменение устаревшего кода всё равно влияет на граф. Только вес снижается.
