# ADR-003: Стратегия fallback при разрешении вызовов

**Статус:** Принято  
**Дата:** 2026-10-13

## Контекст

Не все вызовы можно разрешить статически. Нужна предсказуемая цепочка деградации, которая не блокирует анализ и не теряет важные рёбра.

---

## Часть A: Межсервисные вызовы (RestTemplate / Feign)

### Цепочка разрешения

**Уровень 1 — Строковый литерал (самый частый в `train-ticket`)**
```java
String url = getServiceUrl("ts-station-service");
restTemplate.exchange(url + "/api/v1/stationservice/...", ...)
```
→ Ребро `CALLS_SERVICE`: текущий сервис → `ts-station-service`  
→ Дополнительно: если путь API известен, пытаемся найти соответствующий `@RequestMapping`

**Уровень 2 — Финальная / статическая константа в том же файле**
```java
private static final String ORDER_SERVICE = "ts-order-service";
restTemplate.exchange(getServiceUrl(ORDER_SERVICE) + "/api/...", ...)
```
→ Трейсинг константы в рамках файла → разрешение как уровень 1

**Уровень 3 — `@Value` из конфига**
```java
@Value("${order.service.url}")
private String orderServiceUrl;
restTemplate.exchange(orderServiceUrl + "/api/...", ...)
```
→ Поиск ключа `order.service.url` в `application.yml` / `application.properties`  
→ Если найдено → разрешение как уровень 1  
→ Если не найдено → уровень 4

**Уровень 4 — DiscoveryClient / динамическое обнаружение**
```java
List<ServiceInstance> instances = discoveryClient.getInstances("ts-order-service");
```
→ Парсим имя сервиса из аргумента → ребро `CALLS_SERVICE` если имя строка  
→ Если динамически → уровень 5

**Уровень 5 — Нераскрытый вызов (Black Box Magic)**
```java
restTemplate.exchange(buildUrl(request), ...)  // buildUrl возвращает динамическую строку
```
→ Создаётся узел `UnknownDynamicTarget` с ID `{service}::dynamic#unknown_{hash}`  
→ Ребро `USES_MAGIC_CALL` с весом 1.5  
→ В метаданных: `{"reason": "dynamic_url", "location": "OrderServiceImpl.java:42"}`

---

## Часть B: Внутрисервисные вызовы (Spring DI)

### Цепочка разрешения

**Уровень 1 — Прямой вызов метода**
```java
this.orderRepository.save(order);
```
→ Ребро `CALLS`: текущий метод → `OrderRepository#save`

**Уровень 2 — `@Autowired` / конструкторная инъекция**
```java
@Autowired
private OrderService orderService;
// ...
orderService.createOrder(info);
```
→ Тип поля `OrderService` → ищем `IMPLEMENTS` → находим `OrderServiceImpl`  
→ Ребро `CALLS`: текущий метод → `OrderServiceImpl#createOrder`

**Уровень 3 — Spring AOP / `@Transactional` / прокси**
```java
@Transactional
public void processOrder() { ... }
```
→ Метод помечается аннотацией в метаданных узла  
→ Ребро `USES_MAGIC_CALL` к узлу `UnknownDynamicTarget` типа `AOP_PROXY`  
→ Не блокирует обход, добавляет риск

**Уровень 4 — Рефлексия**
```java
Class.forName("order.OrderServiceImpl").getMethod("createOrder")
```
→ Узел `UnknownDynamicTarget` типа `REFLECTION`  
→ Ребро `USES_MAGIC_CALL` с максимальным весом риска (2.0)

---

## Часть C: Конфиги (docker-compose, Helm)

**Шаг 1:** Парсинг `docker-compose.yml` — извлечение:
- `depends_on` → ребро `SHARES_RESOURCE` (явная зависимость)
- Общие volumes → ребро `SHARES_RESOURCE` между сервисами, использующими том
- Общие сети (networks) → информационно, без рёбер

**Шаг 2:** `environment` переменные с DB-URL → связь с ресурсом  
Паттерн: `SPRING_DATASOURCE_URL=jdbc:mysql://ts-order-mysql:3306/...`  
→ Если два сервиса указывают на один хост БД → ребро `SHARES_RESOURCE`

**Fallback:** если YML не парсится → `PARTIAL_PARSED` для конфиг-слоя, остальной граф не затронут.

---

## Разграничение: USES_MAGIC_CALL vs пропуск

| Ситуация | Результат | Причина |
|---|---|---|
| Нераскрытый **вызов в коде** (рефлексия, dynamic proxy, AOP) | `USES_MAGIC_CALL` + `UnknownDynamicTarget` | Это реальное ребро в архитектуре, просто неизвестный таргет. Пропуск означает потерю ребра. |
| Нераскрытая **env var** (низкий confidence классификатора, ReAct не помог) | Пропустить (нет ребра) | Это проблема классификации, не архитектуры. Лучше не создавать неверное ребро. |
| `@Value` → ключ не найден в конфигах | `USES_MAGIC_CALL` к `UnknownDynamicTarget` | URL существует, но неизвестен статически — это код-уровень неопределённости. |

**Правило:** `USES_MAGIC_CALL` — для кодовых паттернов (что-то вызывается, но мы не знаем куда). Пропуск — для классификационных неудач (неясно, создаёт ли env var ребро вообще).

## Матрица результатов fallback

| Ситуация | Ребро | Узел-цель | Уверенность |
|---|---|---|---|
| Строковый литерал сервиса | `CALLS_SERVICE` | конкретный сервис | Высокая |
| Константа в файле | `CALLS_SERVICE` | конкретный сервис | Высокая |
| `@Value` + найден в конфиге | `CALLS_SERVICE` | конкретный сервис | Средняя |
| `@Value` + не найден | `USES_MAGIC_CALL` | `UnknownDynamicTarget` | Низкая |
| DiscoveryClient (строка) | `CALLS_SERVICE` | конкретный сервис | Средняя |
| DiscoveryClient (динамика) | `USES_MAGIC_CALL` | `UnknownDynamicTarget` | Низкая |
| Полностью динамический URL | `USES_MAGIC_CALL` | `UnknownDynamicTarget` | Нет |
| Рефлексия | `USES_MAGIC_CALL` | `UnknownDynamicTarget` | Нет |

---

## Часть D: Краевые случаи Java/Spring (новые паттерны)

### D.1: `${VAR:default}` — Spring EL с явным дефолтом (уровень 0)

```java
// Gateway YAML:
uri: lb://${ORDER_SERVICE_HOST:ts-order-service}

// Java @Value:
@Value("${station.service.url:http://ts-station-service:12345}")
private String stationUrl;
```

→ Если `VAR` отсутствует в `ENV_INDEX` — берём default-значение напрямую (до уровня 1).  
→ Это уровень 0 (выше уровня 1) — надёжнее строкового литерала, т.к. явно задекларировано.  
→ Правило: перед входом в fallback-цепочку, проверяем наличие `:`-части в Spring EL.

### D.2: `getServiceUrl()` / helper-метод → инлайнинг аргумента (двухпроходной анализ)

279 RestTemplate вызовов в train-ticket используют паттерн:
```java
String url = getServiceUrl("ts-station-service");
restTemplate.exchange(url + "/api/v1/...", POST, ...)
```

`getServiceUrl` объявлен в **базовом классе** (другой файл / ts-common), возвращает `"http://" + serviceName`.

**Критически важно:** tree-sitter парсит один файл изолированно. Файл `OrderServiceImpl.java` вызывает `getServiceUrl(...)`, но объявление метода находится в другом файле. Для корректного инлайнинга нужен двухпроходной анализ:

**Проход 1 (сбор хелперов):** Перед парсингом сервисов обходим все файлы репозитория и собираем хелпер-методы в таблицу:

```python
# Собирается во время первого прохода
HELPER_METHODS: dict[str, HelperFn] = {}

def first_pass_collect_helpers(file_path: Path, tree: ts.Tree) -> None:
    """Ищет методы-хелперы, которые принимают строку и возвращают URL."""
    # Паттерн: метод(String X) -> return "http://" + X
    for method_node in find_methods(tree):
        if returns_string_concat_with_arg(method_node):
            HELPER_METHODS[method_node.name] = HelperFn(
                name=method_node.name,
                arg_index=0,
                strategy="inline_arg"  # аргумент — это сам target
            )
```

**Проход 2 (парсинг вызовов):** При встрече `getServiceUrl(X)` — lookup по `HELPER_METHODS`:

```python
HELPER_INLINING_PATTERNS = {
    # Паттерн: имя_метода(строка) → строка — имя сервиса
    "getServiceUrl": lambda arg: arg,          # возвращает аргумент как target service
    "getUrl": lambda arg: arg,                 # аналог в других сервисах
    "buildUrl": None,                          # buildUrl(request) — динамика, уровень 5
}

def resolve_method_call(call_node, context: FileContext) -> str | None:
    method_name = extract_method_name(call_node)
    arg = extract_first_string_arg(call_node)
    
    # Сначала проверяем predefined паттерны
    if method_name in HELPER_INLINING_PATTERNS:
        fn = HELPER_INLINING_PATTERNS[method_name]
        return fn(arg) if fn and arg else None
    
    # Потом динамически собранные в первом проходе
    if method_name in HELPER_METHODS and arg:
        return HELPER_METHODS[method_name].resolve(arg)
    
    return None  # → уровень 5
```

→ Без первого прохода все 279 вызовов падают на уровень 5 (USES_MAGIC_CALL) — катастрофа для recall.  
→ Первый проход: один раз на весь репозиторий ДО основного парсинга, занимает секунды.

### D.3: `PollThread extends Thread` — поиск вызовов в `run()`

`ts-wait-order-service` делает RestTemplate-вызовы из `Thread.run()`, не из `@RequestMapping` метода.

→ Сканируем не только методы с Spring-аннотациями, но и `run()` при наличии RestTemplate-поля в классе.  
→ Тип ребра: обычный `CALLS_SERVICE`, метаданные: `{"call_context": "thread_run", "polling_interval_min": 5}`.

### D.4: `CommandLineRunner.run()` — НЕ строим межсервисные рёбра

15+ сервисов вставляют seed-данные через `InitData implements CommandLineRunner`.  
Вызовы `service.initOrder(order, null)` — **внутрисервисные**, данные захардкожены.

→ Класс с `implements CommandLineRunner` → помечаем как `arch_role=init_data`.  
→ Вызовы из его `run()` → только `CALLS` рёбра внутри сервиса, не строить `CALLS_SERVICE`.  
→ Иначе: ложные рёбра от seed-методов к другим сервисам.

---

## Часть E: RabbitMQ — асинхронные рёбра

В train-ticket найдены две очереди с чёткими отправителями и получателями:

| Очередь | Отправители | Получатели |
|---|---|---|
| `"email"` | `ts-preserve-service`, `ts-preserve-other-service` | `ts-notification-service` |
| `"food_delivery"` | `ts-food-service` | `ts-delivery-service` |

### Стратегия построения рёбер

**Шаг 1:** Найти константу имени очереди:
```java
public final static String queueName = "email";  // в Queues.java каждого сервиса
```

**Шаг 2:** Найти отправителей (`AmqpTemplate.convertAndSend(queueName, ...)`) и получателей (`@RabbitListener(queues = queueName)`).

**Шаг 3:** Связать по значению константы:
```python
# Если два сервиса используют одно имя очереди — это связь
# sender(email) ∩ receiver(email) → ребро ASYNC_MESSAGE
```

**Ребро:** `ASYNC_MESSAGE` (новый тип, ADR-002).  
**Метаданные:** `{"queue_name": "email", "broker": "rabbitmq", "direction": "async"}`.  
**Вес:** 0.8 — не синхронный вызов, но реальная зависимость.

**Fallback:** Если имя очереди — переменная (не константа) → `USES_MAGIC_CALL` к `UnknownDynamicTarget` типа `ASYNC_QUEUE`.

---

## Часть F: Резолвинг через URL-маршруты (полный путь — ADR-011)

Когда известен полный URL вызова (путь + HTTP-метод), можно резолвить ребро до конкретного контроллер-метода целевого сервиса.

**Шаг 1:** Gateway YAML (`ts-gateway-service/application.yml`) — **первичный источник** RouteIndex.  
Парсится ДО Java-парсера, даёт полную таблицу путей для всех сервисов:
```yaml
- id: order
  uri: lb://${ORDER_SERVICE_HOST:ts-order-service}  # имя сервиса из default
  predicates:
    - Path=/api/v1/orderservice/**                  # prefix пути
```
→ Сразу строим `RouteIndex["ts-order-service"]["*"]["/api/v1/orderservice/**"]`.

**Шаг 2:** Из Java-парсера строится точный `RouteIndex`:
```python
RouteIndex["ts-station-service"]["POST"]["/api/v1/stationservice/stations/namelist"]
    → "ts-station-service::stationinfo.StationInfoController#queryForNameList"
```

**Шаг 2:** При разрешении RestTemplate-вызова:
```java
restTemplate.exchange("http://ts-station-service:12345/api/v1/stationservice/stations/namelist", POST, ...)
```
- Извлекаем: `target_service=ts-station-service`, `path=/api/v1/stationservice/stations/namelist`, `method=POST`
- Ищем в `RouteIndex["ts-station-service"]["POST"][path]`
- Если найдено → ребро `CALLS_SERVICE` до конкретного метода-хендлера (максимальная глубина)
- Если не найдено → ребро до уровня сервиса (уровень 1 выше)

**ENV VAR → URL-путь:**
- `ENV_INDEX` (из docker-compose/ADR-011) разрешает `@Value` в URL
- После получения полного URL → применяем RouteIndex резолвинг

## Последствия

- `UnknownDynamicTarget` узлы включаются в blast radius — повышают recall за счёт precision.
- При появлении baseline можно откалибровать: стоит ли включать `USES_MAGIC_CALL` узлы в обход.
- Цепочка расширяема: новый уровень добавляется без изменения контракта.
- `RouteIndex` строится в фазе 1 Java-парсером; конфиг-парсеры (ADR-011) дополняют `ENV_INDEX`.
