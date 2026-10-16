# ADR-017: Framework Profiles и таксономия ролей эмбеддингов

**Статус:** Принято — **Merged → ADR-022**  
> **Примечание:** Framework Profiles (§A) и роли эмбеддингов (§B) перенесены в ADR-022 (секции F и G). Auto-Rule Loop (§C) перенесён в ADR-014 Tier 4. Этот файл сохранён для справки.  
**Дата:** 2026-10-13  
**Связанные:** ADR-001, ADR-003, ADR-014

## Контекст

Три проблемы:

1. **Расширяемость на языки**: паттерны HTTP-сервера и клиента у Java Spring, Python Flask, Go отличаются. Жёсткий Python-код для каждого фреймворка — не масштабируется.

2. **Дорогой Ollama**: если каждое неизвестное отправлять в LLM — при 50 сервисах × N неизвестных получаем сотни вызовов. При 7-дневном дедлайне это недопустимо.

3. **Семантический поиск без чёткой роли**: непонятно, каким эмбеддингом искать "похожие узлы" — для разных задач нужны разные представления одного и того же кода.

---

## Решение A: Framework Profiles (YAML-конфиги фреймворков)

### Идея

Вместо хардкода Java Spring паттернов в Python-коде — декларативные YAML-профили:

```
src/infrastructure/framework_profiles/
├── java_spring_boot.yaml       # Основной — 47 сервисов train-ticket
├── python_flask.yaml           # ts-avatar-service
├── python_tornado.yaml         # ts-voucher-service
├── go_net_http.yaml            # ts-news-service
└── javascript_express.yaml     # будущие сервисы
```

Парсер загружает профиль по детектированному языку/фреймворку и применяет его паттерны. Новый фреймворк = новый YAML, не новый Python-класс.

### Структура профиля

```yaml
# java_spring_boot.yaml
meta:
  id: java_spring_boot
  language: java
  detection:
    # Как отличить Spring Boot проект от произвольного Java?
    pom_dependency: "spring-boot-starter"
    annotation: "@SpringBootApplication"

# HTTP-сервер: что считать "хендлером" (endpoint метод)
server:
  route_annotations:
    - name: "@RequestMapping"
      path_attr: ["value", "path"]
      method_attr: "method"
    - name: "@GetMapping"
      path_attr: ["value", "path"]
      method: GET
    - name: "@PostMapping"
      path_attr: ["value", "path"]
      method: POST
    - name: "@PutMapping"
      path_attr: ["value", "path"]
      method: PUT
    - name: "@DeleteMapping"
      path_attr: ["value", "path"]
      method: DELETE
  class_route_annotation: "@RequestMapping"  # маппинг на уровне класса — prefix

# HTTP-клиент: что считать межсервисным вызовом
client:
  classes:
    - name: RestTemplate
      call_methods: [exchange, getForObject, postForObject, put, delete]
      url_arg_index: 0
    - name: WebClient
      chain_methods: [get, post, put, delete]
      url_method: uri
    - name: FeignClient
      annotation: "@FeignClient"
      url_attr: url
      name_attr: name  # имя сервиса в Nacos

# Хелперы, которые возвращают URL сервиса по имени
service_url_helpers:
  - method_name: getServiceUrl
    arg_is_service_name: true    # аргумент = имя сервиса
  - method_name: getUrl
    arg_is_service_name: true
  - method_name: buildUrl
    arg_is_service_name: false   # аргумент динамический → уровень 5

# Внедрение зависимостей
dependency_injection:
  field_annotations: ["@Autowired", "@Inject"]
  value_annotation: "@Value"
  value_key_pattern: "\\$\\{([^:}]+)(?::([^}]*))?\\}"  # ${KEY:default}

# Сервисный реестр (service discovery)
service_discovery:
  annotation: "@EnableDiscoveryClient"
  registry: nacos  # | eureka | consul
  app_name_property: "spring.application.name"
  lb_url_prefix: "lb://"  # Spring Cloud LB prefix в URL

# Асинхронные коммуникации
async:
  rabbit_mq:
    sender_class: AmqpTemplate
    sender_method: convertAndSend
    queue_arg_index: 0
    listener_annotation: "@RabbitListener"
    listener_queues_attr: queues

# Аннотации, которые НЕ создают межсервисные рёбра (anti-patterns)
anti_edge_patterns:
  - annotation: CommandLineRunner
    reason: seed_data         # InitData implements CommandLineRunner
  - annotation: "@Scheduled"
    reason: internal_task     # периодическая задача внутри сервиса
  - annotation: "@Transactional"
    reason: internal_tx       # помечаем узел, не создаём ребро
```

```yaml
# python_flask.yaml
meta:
  id: python_flask
  language: python
  detection:
    import: flask
    file_pattern: "app.py"

server:
  route_decorators:
    - name: "@app.route"
      path_arg_index: 0
      method_kwarg: methods
    - name: "@blueprint.route"
      path_arg_index: 0

client:
  libraries:
    - import: requests
      call_methods: [get, post, put, delete, patch, request]
      url_arg_index: 0
    - import: urllib.request
      call_methods: [urlopen, Request]
      url_kwarg: url
    - import: httpx
      call_methods: [get, post, put, delete]
      url_arg_index: 0
  env_url_pattern:
    # os.getenv("SERVICE_URL") с fallback
    function: os.getenv
    fallback_arg_index: 1      # второй аргумент = default value

service_url_helpers: []        # Python-сервисы обычно не используют хелперы
```

```yaml
# go_net_http.yaml
meta:
  id: go_net_http
  language: go
  detection:
    import: "net/http"

server:
  route_functions:
    - package: net/http
      function: HandleFunc
      path_arg_index: 0
    - package: github.com/gorilla/mux
      method_calls: [HandleFunc, GET, POST, PUT, DELETE]

client:
  functions:
    - package: net/http
      function_pattern: "http\\.(Get|Post|Do)"
      url_arg_index: 0
  custom_clients:
    - variable_type: "*http.Client"
      method: Do
      request_url_field: URL
```

### Детекция фреймворка

```python
def detect_framework(service_dir: Path) -> str:
    """Определяем фреймворк по признакам — без запуска кода."""
    # Java/Maven
    if (service_dir / "pom.xml").exists():
        pom = (service_dir / "pom.xml").read_text()
        if "spring-boot-starter" in pom:
            return "java_spring_boot"
    # Python
    py_files = list(service_dir.glob("**/*.py"))
    if py_files:
        first_content = py_files[0].read_text()
        if "from flask" in first_content or "import flask" in first_content:
            return "python_flask"
        if "tornado" in first_content:
            return "python_tornado"
    # Go
    if list(service_dir.glob("**/*.go")):
        return "go_net_http"
    return "unknown"
```

---

## Решение B: Таксономия ролей эмбеддингов (5 ролей)

Одна и та же модель (BGE-M3 или GraphCodeBERT) используется для разных задач — нужно чётко разделить **для чего** каждый вектор и **что** на вход.

### Роль 1: `ENV_PATTERN` — классификация переменных окружения

**Модель:** BGE-M3  
**Задача:** CALLS_SERVICE / SHARES_RESOURCE / ignore / unknown  
**Вход:** `"[TYPE:{value_type}] [SUFFIX:{name_suffix}] {NAME}={value}"`  
**Хранение:** таблица `few_shot_examples`, колонка `role='env_pattern'`

```
[TYPE:http_url] [SUFFIX:_URL] ORDER_SERVICE_URL=http://ts-order-service:12031
→ CALLS_SERVICE (confidence: 0.94)

[TYPE:jdbc_url] [SUFFIX:_URL] SPRING_DATASOURCE_URL=jdbc:mysql://ts-order-mysql:3306
→ SHARES_RESOURCE (confidence: 0.97)
```

### Роль 2: `SHADOW_CODE` — определение закомментированного кода

**Модель:** BGE-M3  
**Задача:** code / text (бинарная)  
**Вход:** текст комментария с убранными маркерами `//` и `/* */`  
**Хранение:** `few_shot_examples`, колонка `role='shadow_code'`

```
"trainFoodRepository.findById(tf.getId())"  → code (0.91)
"This method handles the order creation"    → text (0.88)
"mgo.Dial(MONGODB_URL)"                     → code (0.94)
```

### Роль 3: `CODE_STRUCTURE` — семантическая подпись узла

**Модель:** GraphCodeBERT  
**Задача:** Embedding узла для семантического поиска и group-by-similarity  
**Вход:** `"{class_name}.{method_name}({param_types}) → {return_type}\nAnnotations: {annotations}\n{docstring}"`  
**Хранение:** колонка `embedding` в таблице `nodes` (pgvector, dim=768)

```
"OrderServiceImpl.createOrder(OrderInfo, HttpHeaders) → Response
Annotations: @Service, @Override
Creates a new order and associates it with user account"
→ вектор v ∈ R^768
```

Используется для:
- `find_similar_nodes(node_id, top_k=10)` — "что ещё похоже на этот метод"
- Выявление дублирующих реализаций (ts-order-service vs ts-order-other-service)

### Роль 4: `UNKNOWN_PATTERN` — кластеризация неизвестных для авто-правил

**Модель:** BGE-M3  
**Задача:** Группировка одинаковых паттернов → извлечение правил  
**Вход:** сырой текст неизвестного паттерна (аннотация, URL-шаблон, идиома кода)  
**Хранение:** таблица `unknown_patterns`, колонка `role='unknown_pattern'`

Подробнее — в Решении C (авто-правила).

### Роль 5: `SEMANTIC_SEARCH` — поиск для blast radius

**Модель:** BGE-M3  
**Задача:** "Найди узлы, семантически связанные с изменением X"  
**Вход:** `"{service_name} / {class_name} — {arch_role}: {summary}"`  
**Хранение:** отдельный индекс в pgvector (таблица `nodes`, dim=1024 для BGE-M3)

```
"ts-order-service / OrderServiceImpl — business_logic:
Manages order lifecycle: creation, status updates, cancellation"
→ семантически похожие узлы = кандидаты в blast radius (дополнение к BFS)
```

### Сводная таблица ролей

| Роль | Модель | Вход | Задача | Когда строится |
|---|---|---|---|---|
| `ENV_PATTERN` | BGE-M3 | `[TYPE:..] NAME=val` | Классификация env var | При индексации конфигов |
| `SHADOW_CODE` | BGE-M3 | Текст комментария | Код vs текст | При парсинге comment-узлов |
| `CODE_STRUCTURE` | GraphCodeBERT | Сигнатура + аннотации | Семантика узла | При парсинге Java/Python/Go |
| `UNKNOWN_PATTERN` | BGE-M3 | Сырой неизвестный текст | Кластеризация | При недостаточной уверенности |
| `SEMANTIC_SEARCH` | BGE-M3 | Сервис / роль / summary | Поиск похожих | Фаза 3 (офлайн, после Ollama summarization) |

---

## Решение C: Auto-Rule Loop — кластеризация неизвестных через BGE-M3

### Проблема

Ollama на каждый unknown = N вызовов (дорого и медленно).  
Но многие unknowns — это **один и тот же паттерн** в разных сервисах.

### Идея (предложение пользователя, расширенное)

```
Шаг 1: Накопить unknowns за время парсинга → сохранить в unknown_patterns
Шаг 2: BGE-M3 embed всех unknowns → кластеризация (DBSCAN/cosine)
Шаг 3: Кластер размером ≥ threshold → Ollama ОДИН раз на центроид кластера
Шаг 4: Валидация судьёй (как в ADR-014)
Шаг 5: Сохранить правило → применить ко всему кластеру (и будущим похожим)
```

Вместо N вызовов Ollama — log(N) или sqrt(N) вызовов: по числу кластеров.

### Алгоритм

```python
def auto_rule_loop(unknowns: list[UnknownPattern]) -> list[Rule]:
    """Генерирует правила из накопленных неизвестных паттернов.
    
    Ollama вызывается один раз на кластер, не на каждый unknown.
    """
    # Шаг 1: Эмбеддинги всех неизвестных (батч, BGE-M3)
    embeddings = bge_m3.embed_batch([u.text for u in unknowns])

    # Шаг 2: DBSCAN кластеризация
    # eps=0.15 соответствует ~85% cosine similarity
    labels = dbscan(embeddings, eps=0.15, min_samples=CLUSTER_MIN_SIZE)

    # Шаг 3: Для каждого кластера — один вызов Ollama
    rules = []
    for cluster_id in set(labels):
        if cluster_id == -1:  # шум → одиночные unknowns, пропустить пока
            continue
        members = [u for u, l in zip(unknowns, labels) if l == cluster_id]
        centroid_example = pick_centroid(members, embeddings)

        # Ollama генерирует правило по центроиду (см. ADR-014 промпт)
        rule = ollama_generate_rule(centroid_example, context=members[:3])

        # Валидация судьёй на всём кластере (не только центроиде)
        if judge_validates(rule, sample=members):
            rule.applies_to_cluster = cluster_id
            rules.append(rule)
            save_rule(rule)

    return rules
```

### Параметры кластеризации

```python
CLUSTER_MIN_SIZE = 3         # минимум 3 примера → стоит генерировать правило
COSINE_SIMILARITY_EPS = 0.15 # eps для DBSCAN (1 - cosine): чем меньше, тем строже кластер
MAX_OLLAMA_CALLS = 25        # жёсткое ограничение — не более 25 вызовов за сессию
```

При `MAX_OLLAMA_CALLS=25` и `CLUSTER_MIN_SIZE=3` — система обработает до 75+ unknowns (3 на кластер) одним батчем Ollama. Одиночные паттерны (шум) пропускаются или откладываются на следующий запуск.

### Режимы запуска

| Режим | Триггер | Поведение |
|---|---|---|
| **warmup** | `make db-seed` при первом запуске | Анализирует весь репозиторий, строит полный банк правил |
| **incremental** | При индексации новых/изменённых файлов | Новые unknowns → накапливаются в Redis очереди |
| **batch** | `make generate-rules` (можно запустить вручную) | Перекластеризация всех накопленных unknowns |

### Приоритетная очередь неизвестных

Не все unknowns одинаково ценны. Ранжируем перед кластеризацией:

```python
def unknown_priority(pattern: UnknownPattern) -> float:
    """Высокий приоритет = нужно разобраться быстро."""
    score = 0.0
    # Паттерн встречается часто → высокий приоритет (блокирует много рёбер)
    score += min(pattern.occurrence_count / 10, 1.0) * 0.5
    # Паттерн из критичного сервиса (много входящих рёбер)
    score += pattern.service_centrality * 0.3
    # Паттерн выглядит как HTTP-вызов (может быть реальным ребром)
    score += 0.2 if pattern.looks_like_http_call else 0.0
    return score
```

---

## Быстрый старт (7 дней)

С учётом дедлайна — порядок разработки по убыванию IoU-ценности:

| День | Модуль | Что даёт IoU |
|---|---|---|
| 1–2 | Gateway YAML парсер + `getServiceUrl()` инлайнинг | +~40% рёбер (279 RestTemplate + все маршруты) |
| 2–3 | ENV_INDEX + `${VAR:default}` резолвинг | +~15% рёбер (env var уровни 0–3) |
| 3–4 | RabbitMQ ASYNC_MESSAGE + Shadow pipeline | +~10% рёбер (async + shadow) |
| 4–5 | `warmup` Auto-Rule Loop (BGE-M3 кластеризация → Ollama) | Правила для нестандартных паттернов |
| 5–6 | Framework profiles Python/Go + BFS с shadow | Polyglot сервисы |
| 6–7 | Семантический поиск (SEMANTIC_SEARCH role) + веса blast radius | Качество результата |

---

## Последствия

- Framework profiles в YAML: новый язык = новый файл, не рефакторинг Python.
- BGE-M3 используется для 3 ролей: `ENV_PATTERN`, `SHADOW_CODE`, `UNKNOWN_PATTERN`, `SEMANTIC_SEARCH`.
- GraphCodeBERT — только для `CODE_STRUCTURE` (один раз при индексации).
- Ollama вызывается по числу кластеров, не по числу unknowns → не более 25 вызовов за сессию.
- `unknown_patterns` накапливается между запусками: при повторном запуске правила уже есть для большинства паттернов.
- DBSCAN без обучения: параметр `eps` подбирается на seed-данных, не требует разметки.
