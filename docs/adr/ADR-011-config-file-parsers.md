# ADR-011: Парсеры конфигурационных файлов

**Статус:** Принято  
**Дата:** 2026-10-13

## Контекст

Большая часть межсервисных связей в реальных системах скрыта не в Java-коде, а в конфигурации: docker-compose, Helm, shell-скриптах, .env файлах. Без их анализа граф будет неполным.

### EnvironmentProfile: Tier-управление конфиг-парсерами (ADR-019)

Перед парсингом конфигов определяем доступные источники через `EnvironmentProfile` (ADR-019). Tier определяет какие парсеры активировать:

| Tier | Условие | Активные конфиг-парсеры |
|---|---|---|
| 1 | docker-compose + gateway YAML + pom | Все: compose → gateway → yml → env → Dockerfile |
| 2 | application.yml + pom (нет compose) | yml → env → pom; без compose-сигналов |
| 3 | Только исходники | Только Dockerfile ENV; зависимости из кода |
| 4 | Нет конфигов | Нет конфиг-парсеров; только code analysis |

При Tier 1 `docker-compose.yml` **всегда парсится первым** — он даёт ENV_INDEX и топологию до начала Java-парсинга.

Ошибка парсинга одного конфиг-файла не блокирует остальные (изоляция по уровням — ADR-019).

## Решение

### Приоритет обработки файлов

Приоритет определяет порядок парсинга и ресурсы, вложенные в каждый тип. Высокий приоритет = реализовать в фазе 1–2, низкий = фаза 3 или по необходимости.

| Приоритет | Тип файла | Ценность для графа | Фаза |
|---|---|---|---|
| 🔴 Критично | `*.java` | Основная логика, аннотации, RestTemplate, импорты | 1 |
| 🔴 Критично | `docker-compose.yml` | Все сервисы, env vars с URL, depends_on, volumes | 1 |
| 🔴 Критично | `application.yml` / `.properties` | Резолвинг `@Value`, service URLs, `spring.application.name`, datasource → SHARES_RESOURCE | 1 |
| 🔴 Критично | `nginx.conf` / `*.nginx` | `proxy_pass` → CALLS_SERVICE; `location` → RouteIndex; точка входа снаружи | 1 |
| 🔴 Критично | `.env`, `.env.*` | ENV_INDEX для разрешения URL-переменных | 1 |
| 🟠 Высокий | `kubernetes/svc.yaml` | Полный ServiceRegistry: имена + порты всех сервисов (второй источник после docker-compose) | 1 |
| 🟠 Высокий | `Dockerfile` | ENV инструкции → ENV_INDEX, EXPOSE → порты | 2 |
| 🟠 Высокий | `*.py` (Flask/Tornado сервисы) | `ts-avatar-service`, `ts-voucher-service` | 2 |
| 🟠 Высокий | `*.go` (Go сервисы) | `ts-news-service` | 2 |
| 🟠 Высокий | `package.json` / `*.js` (Node.js) | 1 сервис train-ticket | 2 |
| 🟡 Средний | `Helm values.yaml` / `templates/` | Production конфиг, дублирует docker-compose | 2 |
| 🟡 Средний | `.sh`, `entrypoint.sh` | curl/wget вызовы, вторичные связи | 2 |
| 🟡 Средний | `pom.xml` / `build.gradle` | Границы модулей, общие зависимости | 2 |
| 🟡 Средний | `kubernetes/ingress.yaml` | Внешние маршруты (входные точки наружу) | 2 |
| 🟢 Низкий | SQL-миграции | SHARES_RESOURCE через имена таблиц (слабый сигнал) | 3 |
| 🟢 Низкий | Spring XML (`applicationContext.xml`) | Устаревший стиль, в train-ticket не используется | — |

### Типы файлов и инструменты

| Тип файла | Инструмент | Цель |
|---|---|---|
| `docker-compose.yml` | `PyYAML` + кастомный парсер | Сервисные зависимости, env vars, порты, volumes |
| `nginx.conf` / `*.nginx` | `crossplane` или regex | `proxy_pass` → CALLS_SERVICE; `location` блоки → RouteIndex |
| `Dockerfile` | regex (`ENV`/`ARG`/`EXPOSE` инструкции) | ENV_INDEX пополнение, порты сервисов |
| `Helm values.yaml` / `templates/*.yaml` | `PyYAML` + Jinja2 template resolver | URL-шаблоны сервисов, зависимости чартов |
| `.sh`, `entrypoint.sh`, `Makefile` | regex + токенизатор | `curl`/`wget` URL-вызовы, переменные окружения |
| `.env`, `.env.*` | `python-dotenv` | Переменные с URL-значениями, имена сервисов |
| `application.yml` / `application.properties` | `PyYAML` / `jproperties` | URL свойства, service discovery конфиг, `spring.application.name` → имя сервиса, `datasource.url` → SHARES_RESOURCE |
| `pom.xml` / `build.gradle` | `lxml` / regex | Модульные зависимости, service names |
| `kubernetes/svc.yaml` | `PyYAML` | `kind: Service` → `metadata.name` + `spec.ports` → ServiceRegistry |
| `kubernetes/ingress.yaml` | `PyYAML` | `spec.rules[].host`, `paths[].backend` → внешние маршруты |

### Что извлекаем

#### nginx.conf

В train-ticket: `ts-ui-dashboard/nginx.conf` — единственная точка входа для внешних HTTP запросов.

```nginx
location /api/v1/ {
    proxy_pass http://ts-gateway-service:18888;
}
```

Что извлекаем:
- `proxy_pass http://SERVICE:PORT` → CALLS_SERVICE ребро: nginx → target-service
- `location /prefix/` → RouteIndex: внешний путь → внутренний сервис (через gateway)
- `server { listen PORT; }` → внешний порт nginx (точка входа)

```python
NGINX_PROXY_PATTERN = re.compile(
    r'proxy_pass\s+(https?://[\w\-\.]+(?::\d+)?)'
)
NGINX_LOCATION_PATTERN = re.compile(
    r'location\s+([~*]*\s*[^\s{]+)\s*\{'
)

def parse_nginx_conf(path: Path) -> list[ConfigRelation]:
    text = path.read_text(errors="replace")
    relations = []
    for m in NGINX_PROXY_PATTERN.finditer(text):
        target_url = m.group(1)
        service = extract_service_name_from_url(target_url)
        if service:
            relations.append(ConfigRelation(
                source=path.parent.name,  # ts-ui-dashboard
                target=service,
                relation_type="CALLS_SERVICE",
                confidence="HIGH"
            ))
    return relations
```

**Расположение nginx.conf:** Не только `ts-ui-dashboard/`. Искать рекурсивно — не грепать всё подряд, а сначала найти кандидаты через имя файла (Two-Phase Discovery, ADR-022).

#### application.yml — дополнительные источники

Помимо `@Value` resolution, из `application.yml` извлекаем:

**`spring.application.name`** → официальное имя сервиса. Используется для нормализации NodeId и ServiceRegistry:

```python
# ts-notification-service/application.yml:
#   spring.application.name: ts-notification-service
SERVICE_NAME_PATTERN = re.compile(r'application:\s*\n\s+name:\s*([\w\-]+)')
# или через PyYAML: data["spring"]["application"]["name"]
```

**`spring.datasource.url`** → SHARES_RESOURCE через имя БД-хоста:

```python
# jdbc:mysql://${NOTIFICATION_MYSQL_HOST:ts-notification-mysql}:3306/...
# → target DB host: "ts-notification-mysql"
DATASOURCE_HOST_PATTERN = re.compile(
    r'url:\s*jdbc:\w+://\$\{[^:}]+:([^}]+)\}'   # с дефолтом: ${VAR:default}
    r'|url:\s*jdbc:\w+://([\w\-\.]+):\d+'        # без переменной
)
# Два сервиса с одним DB host → SHARES_RESOURCE ребро между ними
```

**`spring.cloud.nacos.discovery`** → Nacos service mesh регистрация. Сам по себе не создаёт рёбра (сервисы регистрируются через код), но подтверждает имена сервисов и означает что `lb://service-name` маршруты в gateway реальны:

```python
NACOS_PATTERN = re.compile(r'nacos:\s*\n\s+discovery:\s*\n\s+server-addr:\s*(\$\{[^}]+\}|[\w\.\-:,]+)')
node.attrs["uses_nacos"] = True  # метка: имя в Nacos = spring.application.name
```

**`spring.rabbitmq.host`** / **`spring.rabbitmq.port`** → подтверждает что сервис использует RabbitMQ:
```python
node.attrs["uses_rabbitmq"] = True  # → при нахождении AmqpTemplate в коде — HIGH confidence
```

#### Kafka (паттерн для других репо)

В train-ticket Kafka закомментирована в docker-compose, `@KafkaListener` не используется. Но общий паттерн для других репо:

```yaml
# application.yml
spring:
  kafka:
    bootstrap-servers: ${KAFKA_HOST:localhost}:9092
    consumer.group-id: my-group
```

```java
// Producer
@Autowired KafkaTemplate<String, String> kafkaTemplate;
kafkaTemplate.send("topic-name", message);

// Consumer
@KafkaListener(topics = "topic-name", groupId = "my-group")
public void consume(String message) { ... }
```

```python
KAFKA_SEND_PATTERN = re.compile(
    r'kafkaTemplate\.send\s*\(\s*["\']([^"\']+)["\']'
)
KAFKA_LISTEN_PATTERN = re.compile(
    r'@KafkaListener\s*\(.*?topics\s*=\s*["\']([^"\']+)["\']'
)
# Результат: ASYNC_MESSAGE ребро с metadata.queue_name=topic, metadata.broker="kafka"
```

#### Dockerfile

```dockerfile
# Пример
FROM openjdk:11-slim
ENV TS_STATION_SERVICE_URL=http://ts-station-service:12345
ENV SERVER_PORT=12306
ARG PROFILE=dev
EXPOSE 12306
```

Извлекаем:
- `ENV KEY=value` и `ENV KEY value` → добавляем в `ENV_INDEX[service_name]`
- `ARG` — только если имеет дефолтное значение с URL-паттерном
- `EXPOSE port` → регистрируем порт сервиса (используется при резолвинге URL)

После извлечения — **кросс-референс** с другими файлами:
- Имя переменной из `ENV` ищем в Java-коде (`@Value("${KEY}")`): прямой матч → CALLS_SERVICE
- Значение ищем в docker-compose/Helm: если совпадает — подтверждаем существующее ребро
- Имя хоста из значения ищем среди известных сервисов: если матч → строим ребро

```python
# Пример кросс-референса
# Dockerfile: ENV TS_STATION_SERVICE_URL=http://ts-station-service:12345
# Java:       @Value("${TS_STATION_SERVICE_URL}") private String stationUrl;
# → Ребро CALLS_SERVICE: текущий сервис → ts-station-service  (CONFIRMED)
```

#### docker-compose.yml

```yaml
# Пример
services:
  ts-order-service:
    environment:
      SPRING_DATASOURCE_URL: jdbc:mysql://ts-order-mysql:3306/ts
      TS_STATION_SERVICE_URL: http://ts-station-service:12345
    depends_on: [ts-order-mysql, ts-auth-service]
    networks: [ts-backend]
```

Извлекаем:
- `depends_on` → ребро `SHARES_RESOURCE` между сервисами
- Env var с URL-паттерном (`*_URL`, `*_HOST`, `*_ENDPOINT`) → ребро `CALLS_SERVICE` если содержит имя другого сервиса
- Env var без URL, но с именем сервиса → метаданные для разрешения `@Value` в Java
- Общие networks → группировка (не ребро, метаданные)
- Shared volumes → ребро `SHARES_RESOURCE` между сервисами, монтирующими один том

**Индекс env vars:**
```python
# При парсинге docker-compose строим словарь:
ENV_INDEX: dict[str, dict[str, str]] = {
    "ts-order-service": {
        "SPRING_DATASOURCE_URL": "jdbc:mysql://ts-order-mysql:3306/ts",
        "TS_STATION_SERVICE_URL": "http://ts-station-service:12345",
    }
}
# Используется для разрешения @Value в ADR-003 (уровень 3)
```

#### Shell-скрипты (`.sh`, `entrypoint.sh`)

Паттерны поиска:
```python
SHELL_PATTERNS = [
    r'curl\s+.*?(https?://[\w\-\./:]+)',      # curl http://ts-service:port/api/...
    r'wget\s+.*?(https?://[\w\-\./:]+)',      # wget http://...
    r'\$\{?([A-Z_]+(?:_URL|_HOST|_ENDPOINT))\}?',  # ${SERVICE_URL}
    r'"(https?://[\w\-\.]+:\d+/[^"]*)"',      # "http://hostname:port/path"
    r"(ts-[\w-]+)",                            # прямое упоминание имени сервиса
]
```

#### Kubernetes / Helm

```yaml
# templates/deployment.yaml
env:
  - name: ORDER_SERVICE_URL
    value: "http://ts-order-service.default.svc.cluster.local:12345"
```

Извлекаем:
- `.svc.cluster.local` URL → разбираем на `{service}.{namespace}` → имя сервиса
- `Service` kind → регистрация имён сервисов и их портов
- `Ingress` kind → внешние маршруты (для визуализации входных точек)

### URL-маршруты: связь кода с конфигом

Ключевая идея: **связывать вызовы через полный путь URL**, а не только имя сервиса.

```
RestTemplate.exchange(
    "http://ts-station-service:12345/api/v1/stationservice/stations/namelist",
    POST
)
```

Шаг 1: Из Java-парсера получаем: `target_service="ts-station-service"`, `path="/api/v1/stationservice/stations/namelist"`, `method=POST`

Шаг 2: Из `@RequestMapping` Java-парсера строим индекс роутов:
```python
ROUTE_INDEX: dict[str, str] = {
    "ts-station-service:POST:/api/v1/stationservice/stations/namelist":
        "ts-station-service::stationinfo.StationInfoController#queryForNameList"
}
```

Шаг 3: Резолвим ребро до конкретного метода-хендлера:
```
ts-order-service::order.OrderServiceImpl#getSoldTickets
    → CALLS_SERVICE (через URL) →
ts-station-service::stationinfo.StationInfoController#queryForNameList
```

Это максимальная глубина межсервисного ребра (ADR-005: "как можно глубже").

### ENV VAR resolution — цепочка

```
1. Переменная встречается в коде: @Value("${TS_STATION_URL}")
2. Ищем в ENV_INDEX["ts-order-service"]["TS_STATION_URL"]
3. Находим значение: "http://ts-station-service:12345"
4. Извлекаем имя сервиса: "ts-station-service"
5. Если известен path → резолвим до конкретного хендлера (см. выше)
6. Если path неизвестен → ребро CALLS_SERVICE до сервиса-уровня
7. Если env var не найдена → USES_MAGIC_CALL (ADR-003, уровень 4)
```

### Структура парсеров

```
src/adapters/parsers/config/
├── base.py                  # ConfigParser(ABC): parse(path) → list[ConfigRelation]
├── docker_compose.py        # DockerComposeParser
├── helm_parser.py           # HelmParser
├── shell_parser.py          # ShellScriptParser
├── env_parser.py            # EnvFileParser
├── spring_props.py          # ApplicationYmlParser / ApplicationPropertiesParser
└── route_index.py           # RouteIndex: строится из Java-парсера, используется здесь
```

```python
@dataclass
class ConfigRelation:
    source: str          # имя сервиса-источника
    target: str          # имя сервиса-цели или URL
    relation_type: str   # CALLS_SERVICE | SHARES_RESOURCE
    path: str | None     # URL-путь, если известен
    env_var: str | None  # через какую переменную
    confidence: str      # HIGH | MEDIUM | LOW
```

### Python-сервисы: urllib и env var паттерн

`ts-voucher-service` демонстрирует типичный Python-паттерн без service discovery:

```python
# Паттерн 1: дефолтный URL + env var override
order_url = 'http://ts-order-service:12031'  # строковый литерал — уровень 1
if os.getenv("ORDER_SERVICE_URL") is not None:
    order_url = os.getenv("ORDER_SERVICE_URL")  # env var override — уровень 3

# Паттерн 2: прямой вызов urllib
req = urllib.request.Request(url=order_url + '/api/v1/orderservice/order/'+orderId)
response = urllib.request.urlopen(req)
```

Стратегия парсинга Python:
- `tree-sitter-python` находит string literals с `ts-*` паттерном → `CALLS_SERVICE` (уровень 1)
- `os.getenv("ORDER_SERVICE_URL")` → ищем `ORDER_SERVICE_URL` в `ENV_INDEX` (уровень 3)
- `urllib.request.Request(url=...)` или `requests.get(url=...)` → аналог RestTemplate, применяем ту же fallback-цепочку

```python
PYTHON_HTTP_CALL_PATTERNS = [
    # urllib.request
    r'urllib\.request\.(urlopen|Request)\s*\(\s*url\s*=\s*([^\)]+)',
    # requests library
    r'requests\.(get|post|put|delete|patch)\s*\(\s*([^\)]+)',
    # Flask redirect/send_request
    r'requests\.Session\(\)',
]
```

**Env var паттерн в Python** (добавить в `ENV_INDEX` приоритет для Python-сервисов):
```python
# ts-voucher-service использует:
VOUCHER_MYSQL_HOST, VOUCHER_MYSQL_PORT, ORDER_SERVICE_URL, ORDER_OTHER_SERVICE_URL
```

### FreeMarker-шаблоны (`.ftl`)

`ts-notification-service` использует FreeMarker для email-уведомлений:
```
ts-notification-service/src/main/resources/templates/
├── preserve_success.ftl       # Бронирование создано
├── order_create_success.ftl   # Заказ создан
├── order_changed_success.ftl  # Заказ изменён
└── order_cancel_success.ftl   # Заказ отменён
```

- `.ftl` файлы — **не источники рёбер** (шаблоны отображения, не зависимости).
- Их наличие помечает `ts-notification-service` как узел с `arch_role=notification`, `PACKAGE_ROLE=templates`.
- Вызовы `ts-notification-service` из других сервисов уже строятся через RestTemplate в Java-коде.

### SecurityConfig: маршруты и роли доступа

Каждый Java-сервис содержит `SecurityConfig extends WebSecurityConfigurerAdapter` с JWT-аутентификацией.

Что извлекаем:
```java
// ts-order-service SecurityConfig:
.antMatchers(HttpMethod.POST, "/api/v1/orderservice/order").hasAnyRole("ADMIN", "USER")
.antMatchers(HttpMethod.POST, "/api/v1/orderservice/order/admin").hasAnyRole("ADMIN")
.antMatchers("/api/v1/orderservice/order/**").permitAll()
```

→ Дополняем `RouteIndex` полем `requires_auth` и `allowed_roles`:
```python
ROUTE_INDEX["ts-order-service"]["POST"]["/api/v1/orderservice/order"] = {
    "handler": "ts-order-service::order.OrderController#createOrder",
    "requires_auth": True,
    "allowed_roles": ["ADMIN", "USER"]
}
```

Это не влияет на граф рёбер напрямую, но обогащает метаданные узлов-хендлеров для анализа доступа.

## Обоснование

- Без конфиг-парсеров теряем ~20–30% рёбер (особенно `SHARES_RESOURCE` через БД и `CALLS_SERVICE` через env vars).
- URL-маршрутный резолвинг даёт самые глубокие (и точные) межсервисные рёбра.
- `ENV_INDEX` решает проблему `@Value` → конкретный сервис (ADR-003 уровень 3).

### ENV_INDEX: fuzzy lookup через BGE-M3

Точный lookup `ENV_INDEX[service][VAR_NAME]` не находит переменную если имя нестандартное.  
При промахе — запасной поиск по cosine similarity среди известных var names:

```python
def resolve_env_var(service: str, var_name: str,
                    env_index: dict, rule_engine: RuleEngine) -> str | None:
    """Точный lookup → fuzzy → None."""
    # 1. Точный lookup (O(1))
    value = env_index.get(service, {}).get(var_name)
    if value:
        return value

    # 2. Fuzzy: ищем похожее имя переменной среди известных
    # Поиск в rules table (scope=env_var, role=ENV_PATTERN)
    match = rule_engine.match(scope="env_var", text=var_name,
                              fuzzy_threshold=0.80)
    if match and match.match_type == "fuzzy":
        # Нашли похожую переменную → берём её значение
        similar_var = match.rule.example_text
        return env_index.get(service, {}).get(similar_var)

    return None
```

Пример: `ORDER_BACKEND` → fuzzy match к `ORDER_SERVICE_URL` (similarity=0.83) → берём значение `ORDER_SERVICE_URL`. Без этого — USES_MAGIC_CALL.

### RouteIndex: приоритеты источников (единая таблица)

Все источники RouteIndex ранжируются по confidence. Ниже — **единственная каноническая таблица** (ADR-025 §A.2 и ADR-028 §F ссылаются на это место).

| Источник | Confidence | Описание |
|---|---|---|
| `gateway YAML` (Path predicates + service mapping) | 1.00 | Прямое соответствие путь → сервис |
| `@RequestMapping` / `@Get/PostMapping` (Java контроллер) | 0.95 | Объявление endpoint в коде |
| Angular `$http.get/post` (ts-ui-dashboard) | 0.95 | Frontend вызов → маршрут |
| `MockMvc` тест (ADR-025 §A.4) | 0.80 | Верификация endpoint, не источник рёбер |
| legacy jQuery `$.ajax` (ts-ui-dashboard/js/) | 0.50 | Старый JS без типизации |
| `readme.txt` legacy nginx `proxy_pass` (ADR-028 §F) | 0.40 | Pre-gateway архив; проверять на dead refs |
| `readme.txt` code snippets | 0.25 | Неструктурированный текст; только как hint |

**Правило применения:** при конфликте двух источников для одного маршрута побеждает источник с более высоким confidence.

### RouteIndex: glob и path-variable matching

RouteIndex — не flat dict, а matcher с поддержкой паттернов:

```python
class RouteIndex:
    """Резолвинг URL-пути к конкретному handler-методу.
    
    Источники (в порядке приоритета):
    1. Gateway YAML: glob паттерны (/api/v1/orderservice/**)
    2. Java @RequestMapping: path variables (/order/{id})
    """

    def resolve(self, service: str, method: str, path: str) -> str | None:
        """Найти handler для service+method+path.
        
        Попытки: точное совпадение → path variables → glob → prefix.
        """
        candidates = self._routes.get(service, {}).get(method, [])

        # 1. Точное совпадение
        for route in candidates:
            if route.pattern == path:
                return route.handler_node_id

        # 2. Path variables: /order/{id} совпадает с /order/123
        for route in candidates:
            if path_variable_match(route.pattern, path):
                return route.handler_node_id

        # 3. Glob (из gateway): /api/v1/orderservice/** совпадает с /api/v1/orderservice/order
        for route in candidates:
            if glob_match(route.pattern, path):
                return route.handler_node_id

        # 4. Prefix (fallback): /api/v1/orderservice → сервис-уровень
        for route in candidates:
            if path.startswith(route.prefix):
                return route.service_node_id  # не метод, а сервис

        return None
```

`path_variable_match("/order/{id}", "/order/123")` → True (regex `^/order/[^/]+$`).  
`glob_match("/api/v1/**", "/api/v1/orderservice/order")` → True (fnmatch или pathlib).

## Последствия

- `RouteIndex` строится на этапе парсинга Java (фаза 1), конфиг-парсеры используют его в фазе 2.
- При неполных Helm-шаблонах (с `{{ .Values.xxx }}`) — парсинг без Jinja-резолвинга даст частичный результат: `PARTIAL_PARSED`.
- Shell-паттерны (из `rules` table, ADR-018) дают MEDIUM confidence — не ломают граф, но учитываются при ранжировании.
- ENV_INDEX fuzzy lookup предотвращает USES_MAGIC_CALL для нестандартных имён переменных.
- RouteIndex glob matching необходим для gateway YAML-маршрутов (`Path=/api/v1/**`).
