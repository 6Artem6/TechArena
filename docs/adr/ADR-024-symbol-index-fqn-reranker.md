# ADR-024: SymbolIndex с FQN-резолвингом, regex+bi-encoder реранкер, найденные аномалии

**Статус:** Принято — **Merged → ADR-023**  
> **Примечание:** FQN SymbolIndex (§A), bi-encoder реранкер (§B) и аномалии train-ticket (§C) перенесены в ADR-023 секции C, H и I. Этот файл сохранён для справки.  
**Дата:** 2026-10-13  
**Связанные:** ADR-003, ADR-021, ADR-023

---

## Контекст

Реальная проверка train-ticket выявила критическую ошибку в стратегии резолвинга и ряд настоящих аномалий в репо.

---

## A. Критическая ошибка: SymbolIndex без FQN

### Проблема

В train-ticket **5 разных файлов** называются `Queues.java` (по одному в каждом сервисе), и все содержат поле `queueName` с разными значениями:


| Сервис                    | FQN                           | queueName         |
| ------------------------- | ----------------------------- | ----------------- |
| ts-notification-service   | `notification.config.Queues`  | `"email"`         |
| ts-preserve-service       | `preserve.config.Queues`      | `"email"`         |
| ts-preserve-other-service | `preserveOther.config.Queues` | `"food_delivery"` |
| ts-food-service           | `foodsearch.config.Queues`    | `"email"`         |
| ts-delivery-service       | `delivery.config.Queues`      | `"food_delivery"` |


Если SymbolIndex хранит `(class_name, field_name) → value` без пакета — все 5 перезаписывают друг друга. В нашем первом тесте `ts-food-service` ошибочно получил значение `"email"` вместо `"food_delivery"`.

**Правильный результат после FQN-резолвинга через imports:**

```
Queue "email":     SEND = {ts-preserve-service, ts-preserve-other-service, ts-notification-service}
                   LISTEN = {ts-notification-service}
                   *** CIRCULAR: ts-notification-service ***

Queue "food_delivery":  SEND = {ts-food-service}
                        LISTEN = {ts-delivery-service}
                        OK
```



### Решение: FQN через import-разрешение

```python
@dataclass
class StaticFieldEntry:
    fqn: str          # "notification.config.Queues.queueName"
    class_name: str   # "Queues"  (короткое)
    field_name: str   # "queueName"
    package: str      # "notification.config"
    value: str        # "email"

class SymbolIndex:
    def __init__(self):
        self._by_fqn: dict[str, StaticFieldEntry] = {}
        self._by_short: dict[tuple[str, str], list[StaticFieldEntry]] = {}

    def add_field(self, entry: StaticFieldEntry):
        self._by_fqn[entry.fqn] = entry
        key = (entry.class_name, entry.field_name)
        self._by_short.setdefault(key, []).append(entry)

    def resolve_ref(
        self,
        cls_ref: str,       # "Queues"
        field: str,         # "queueName"
        imports: dict[str, str],  # short → fqn из imports файла
    ) -> str | None:
        # Попытка 1: через imports файла (точное)
        fqn_cls = imports.get(cls_ref, cls_ref)
        fqn = f"{fqn_cls}.{field}"
        entry = self._by_fqn.get(fqn)
        if entry:
            return entry.value

        # Попытка 2: wildcard import (import delivery.config.*)
        for imp_fqn in imports.values():
            if imp_fqn.endswith('.*'):
                candidate = f"{imp_fqn[:-1]}{cls_ref}.{field}"
                entry = self._by_fqn.get(candidate)
                if entry:
                    return entry.value

        # Попытка 3: same package (нет импорта — тот же пакет)
        candidates = self._by_short.get((cls_ref, field), [])
        if len(candidates) == 1:
            return candidates[0].value  # единственный кандидат

        # Попытка 4: неоднозначно — bi-encoder реранкер (§B ниже)
        if len(candidates) > 1:
            return None  # передаём реранкеру

        return None
```



### Сбор FQN при парсинге

```python
def index_static_fields(file_path: Path, tree, source_text: str) -> list[StaticFieldEntry]:
    # Пакет
    pkg_node = find_package_declaration(tree)
    pkg = source_text[pkg_node.start_byte:pkg_node.end_byte].strip() if pkg_node else ""

    # Класс
    class_node = find_first_class(tree)
    if not class_node: return []
    cls_name = get_node_name(class_node, source_text)
    fqn_cls = f"{pkg}.{cls_name}" if pkg else cls_name

    entries = []
    for field_node in find_static_string_fields(tree):
        fname = get_field_name(field_node, source_text)
        fval = get_field_string_value(field_node, source_text)
        if fname and fval:
            entries.append(StaticFieldEntry(
                fqn=f"{fqn_cls}.{fname}",
                class_name=cls_name,
                field_name=fname,
                package=pkg,
                value=fval,
            ))
    return entries
```

---



## B. Regex + Bi-encoder реранкер для неоднозначных ссылок



### Когда нужен

FQN-резолвинг не помогает в случаях:

- Wildcard import: `import ts.common.*` — не ясно какой класс
- Ссылка через поле объекта: `service.getQueueName()` — runtime
- Одинаковые FQN в разных версиях модуля
- `UNRESOLVED:ClassName.fieldName` — класс не найден ни по imports, ни в same package



### Стратегия: кандидаты → bi-encoder → реранкер

```
Шаг 1: Regex генерирует кандидатов
  Берём все известные значения нужного типа:
  - для QUEUE_NAME → все значения из SymbolIndex где field_name ≈ "queue*" или "topic*"
  - для SERVICE_NAME → ServiceRegistry
  - для CONFIG_KEY → ENV_INDEX ключи

Шаг 2: Bi-encoder кодирует запрос и кандидатов
  Запрос = контекст использования:
    "[SVC:ts-food-service][FILE:RabbitSend][METHOD:send][CALL:convertAndSend(Queues.queueName,val)]"
  Кандидаты = контексты объявления:
    "[SVC:ts-food-service][FILE:Queues][CLASS:Queues][FIELD:queueName][VALUE:email]"
    "[SVC:ts-delivery-service][FILE:Queues][CLASS:Queues][FIELD:queueName][VALUE:food_delivery]"
    "[SVC:ts-preserve-service][FILE:Queues][CLASS:Queues][FIELD:queueName][VALUE:email]"

Шаг 3: Cosine similarity → топ-K (K=3)

Шаг 4: Реранкер
  Эвристика 1: Предпочесть кандидата из ТОГО ЖЕ сервиса (same-service bonus +0.2)
  Эвристика 2: Предпочесть совпадение по импорту (import-match bonus +0.15)
  Эвристика 3: Предпочесть кандидата у которого service ∈ known callers
```

```python
@dataclass
class ResolutionCandidate:
    value: str                  # "email" / "food_delivery"
    source_fqn: str             # откуда взято
    source_service: str         # в каком сервисе объявлено
    embedding_sim: float        # cosine similarity
    rerank_score: float         # после реранкинга

def rerank_candidates(
    candidates: list[ResolutionCandidate],
    use_context: UseContext,    # сервис и файл использования
) -> ResolutionCandidate | None:
    for c in candidates:
        # same-service bonus
        if c.source_service == use_context.service:
            c.rerank_score = c.embedding_sim + 0.2
        else:
            c.rerank_score = c.embedding_sim
    
    # Сортируем по rerank_score
    candidates.sort(key=lambda x: x.rerank_score, reverse=True)
    
    best = candidates[0] if candidates else None
    if best and best.rerank_score >= 0.65:
        return best
    return None  # неуверен → UNRESOLVED, не создавать ребро
```

**Результат для train-ticket:**

- `ts-food-service.RabbitSend.convertAndSend(Queues.queueName, ...)`:
  - same-service: `ts-food-service.Queues.queueName = "email"` → sim + 0.2 = **winner**
- `ts-delivery-service.RabbitReceive.@RabbitListener(queues = Queues.queueName)`:
  - same-service: `ts-delivery-service.Queues.queueName = "food_delivery"` → winner

---



## C. Найденные аномалии в train-ticket



### C.1 CIRCULAR: ts-notification-service на очереди "email"

```
ts-notification-service/notification/mq/RabbitSend.java:
    convertAndSend(Queues.queueName, val)   // → "email"
ts-notification-service/notification/mq/RabbitReceive.java:
    @RabbitListener(queues = Queues.queueName)  // → "email"
```

Сервис одновременно шлёт и слушает свою же очередь. Возможные интерпретации:

1. Намеренно: notification-service сам себе ставит задачи в очередь для асинхронной обработки
2. Ошибка конфигурации: `RabbitSend` должен был слать в другую очередь

**Действие в графе:** создавать CIRCULAR пометку на ребре; не считать это dead message.

### C.2 Hardcoded IP вместо hostname в 5 сервисах

```yaml
# ts-route-service, ts-price-service, ts-order-service, 
# ts-station-food-service, ts-wait-order-service
spring.datasource.url: jdbc:mysql://${HOST:10.176.122.1}:3306/ts
```

Default — IP-адрес разработчика, не docker-compose hostname. При запуске через Docker Compose без override переменной ENV — подключение провалится (10.176.122.1 недоступен).

**В графе:** эти сервисы помечаются `missing_db_config=True`; не создаём SHARES_RESOURCE через `10.176.122.1` (IP не является именем сервиса).

**В technical_debt[]:**

```json
{
  "kind": "HARDCODED_IP",
  "node": "ts-route-service",
  "detail": "datasource default host=10.176.122.1, not a docker-compose service name"
}
```



### C.3 @Value ключи без объявления и без дефолта


| Сервис                  | Ключ                           | Где используется                           |
| ----------------------- | ------------------------------ | ------------------------------------------ |
| ts-notification-service | `${test_send_mail_user}`       | NotificationController — тестовый эндпоинт |
| ts-common               | `${swagger.controllerPackage}` | SwaggerConfig — Swagger UI конфиг          |


Оба — тестовые/вспомогательные. Не критичны для blast radius, но добавляем в `technical_debt[]` как MISSING_CONFIG.

### C.4 ts-consign-mysql не в основном docker-compose

```yaml
# ЕСТЬ в:
deployment/docker-compose-manifests/quickstart-docker-compose.yml  ← quickstart
deployment/docker-compose-manifests/docker-compose-with-jaeger.yml  ← jaeger

# НЕТ в:
docker-compose.yml  ← основной compose
```

Основной docker-compose не содержит `ts-consign-mysql`. Это значит datasource default из `ts-consign-service/application.yml` (`${CONSIGN_MYSQL_HOST:ts-consign-mysql}`) — сервис не запустится без quickstart compose.

**Действие:** При парсинге искать все файлы docker-compose (не только корневой). Two-Phase: найти все `docker-compose*.yml` → объединить ServiceRegistry.

---



## D. Полный алгоритм ENV var lookup chain (обновлённый)

```
@Value("${key:default}") → resolve(key, service, context):

  1. ENV_INDEX[service][key]              точное совпадение → HIGH conf (1.0)
  2. ENV_INDEX[service][normalize(key)]   нормализация ключа (-→_, .→_) → HIGH conf (0.95)
  3. application.yml[service][key]        из resources/application.yml → HIGH conf (0.95)
  4. quickstart-docker-compose.yml[key]   расширенный compose → MED conf (0.8)
  5. ENV_INDEX[*][key]                    другой сервис с таким же ключом → MED conf (0.7)
  6. BGE-M3 fuzzy в ENV_INDEX             cosine ≥ 0.80 → MED conf (0.7 × sim)
  7. default из @Value("${key:DEFAULT}")  → LOW conf (0.6)
  8. USES_MAGIC_CALL                      → conf = 0.0

Шаги 5–6 применяются только если ключ не найден в сервисе-источнике.
```

**Важно:** шаг 4 — проверять ВСЕ docker-compose файлы в репо (Two-Phase: найти все `*docker-compose*.yml` → объединить).

---



## E. Итог: что исправляем в реализации


| Проблема                                   | Исправление                                                     |
| ------------------------------------------ | --------------------------------------------------------------- |
| SymbolIndex без FQN → конфликт Queues.java | FQN = package + class + field; resolve через imports            |
| Same-class name в разных пакетах           | rerank_candidates() с same-service bonus                        |
| ts-notification-service CIRCULAR           | помечать CIRCULAR, не считать dead message                      |
| IP-адреса как default в datasource         | не создавать SHARES_RESOURCE через IP; `missing_db_config=True` |
| Только корневой docker-compose             | искать все `*docker-compose*.yml` через Two-Phase               |
| @Value без дефолта и без compose → MISS    | добавлять в technical_debt[MISSING_CONFIG]                      |


