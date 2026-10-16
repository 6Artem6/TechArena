# ADR-025: Тестовые файлы как источник данных, моки вне тестов, рекурсивные комментарии

**Статус:** Принято  
**Дата:** 2026-10-13  
**Связанные:** ADR-016, ADR-019, ADR-021, ADR-023

---

## Контекст

Реальная проверка train-ticket выявила три самостоятельных паттерна, требующих явной политики.

---

## A. Тестовые файлы: исключение с исключениями

### A.1 Что есть в тестах train-ticket

Все тесты находятся в `src/test/java/`, используют `@Mock + @InjectMocks + Mockito` (паттерн unit-test с ручными моками).

| Факт | Значение |
|---|---|
| Тип тестов | JUnit + Mockito, **НЕ** `@SpringBootTest` (1 исключение — пустой `contextLoads()`) |
| `@WebMvcTest` | 0 — нет слоёв |
| `@Mock RestTemplate` | 92 моковых RestTemplate-объекта в тестах |
| MockMvc URL-пути | **250 уникальных путей** (`/api/v1/travelservice/...`) |
| `@RequestMapping` в main | 290 путей |

### A.2 Что ИСКЛЮЧАЕМ из графа

```python
def should_skip_file(file_path: Path) -> bool:
    parts = file_path.parts
    # Стандартное исключение — тестовые директории
    if "src/test" in str(file_path):
        return True
    # node_modules — только для JS-сервисов
    if "node_modules" in parts:
        return True
    # old-docs — архивный материал, не часть системы
    if "old-docs" in parts:
        return True
    return False
```

**Почему исключаем `src/test/`:**
- `@Mock RestTemplate` заменяет реальный RestTemplate → вызовы внутри теста — это fixture-данные, не настоящие зависимости
- `Mockito.when(restTemplate.exchange(...)).thenReturn(...)` — не CALLS_SERVICE, это stub
- Тест вызывает `travelServiceImpl.getRouteByTripId(...)` напрямую, минуя HTTP

### A.3 Что ИЗВЛЕКАЕМ из тестов (ценные данные)

Тесты содержат **250 жёстко прописанных URL-путей** вида `/api/v1/travelservice/trips/left`.  
Это **дополнительный источник для RouteIndex** — не для построения рёбер, а для верификации.

```python
def extract_test_route_hints(test_file: Path) -> list[str]:
    """
    MockMvcRequestBuilders.get("/api/v1/...") → извлечь путь.
    Использовать ТОЛЬКО как подтверждение RouteIndex, не как первичный источник.
    """
    MOCK_MVC_PATTERN = re.compile(
        r'MockMvcRequestBuilders\.\w+\("(/[^"]+)"'
    )
    text = test_file.read_text(errors='replace')
    return MOCK_MVC_PATTERN.findall(text)
```

**Правило применения:**
- Если путь `/api/v1/orderservice/order` найден в `MockMvc` теста `ts-order-service` → подтверждаем этот endpoint в RouteIndex с `source=test_hint`
- Если путь есть только в тестах, но нет в main `@RequestMapping` → добавляем в `technical_debt[ORPHAN_TEST_URL]`
- Приоритет источников RouteIndex: `gateway.yml` > `@RequestMapping` > `test_hint`

### A.4 Нет моков в main-коде

В `src/main/` train-ticket **нет ни одного** `*Mock.java`, `*Stub.java`, `*Fake.java` (кроме `old-docs/`).

| Директория | Файлы с Mock/Stub в main |
|---|---|
| `old-docs/` | `Item.java` (устаревший, исключаем) |
| Активные сервисы | 0 |

Вывод: в train-ticket mock-инфраструктура строго изолирована в `src/test/`. Правило `should_skip_file()` достаточно.

---

## B. Сервисы в тестовых директориях

В `src/test/` нет `@RestController` или `@SpringBootApplication`. Единственный `@SpringBootTest` — пустой:

```java
// ts-wait-order-service/src/test/java/waitorder/WaitOrderApplicationTests.java
@SpringBootTest
class WaitOrderApplicationTests {
    @Test
    void contextLoads() { }  // smoke test, нет бизнес-логики
}
```

**Правило:** если файл в `src/test/` содержит `@SpringBootApplication` или `@RestController` — это нетипично, логировать как предупреждение, не индексировать в граф.

---

## C. Закомментированный код: рекурсивные уровни и классификация

### C.1 Реальные паттерны из train-ticket

**В Java-сервисах** `// //` не встречается (0 совпадений). Но **в Go и JS** — есть:

| Файл | Паттерн | Глубина | Семантика |
|---|---|---|---|
| `ts-news-service/main.go:24-25` | `// // db := session.DB("xtest")` | 2 | Первая попытка внутри уже закомментированного метода |
| `ts-ui-dashboard/myflow.js` | `// //alert(...)`, `// // url += ...` | 2 | Закомментированные debug-вызовы UI |
| `ts-ui-dashboard/upload.js:4` | `// // 阿里云账号...` | 2 | Vendor-документация (китайский) — не код |
| `ts-ui-dashboard/static/assets/js/angular.js` | `////...` (десятки) | — | Декоративные разделители секций — не вложенный код |

**ts-news-service/main.go — ключевая находка:**

```go
// Весь файл: активный код — заглушка (hardcoded JSON), MongoDB полностью закомментирован:

//const (
//    MONGODB_URL = "ts-news-mongo:27017"   ← уровень 1: shadow SHARES_RESOURCE
//)

//func getNews(val string) string {          ← уровень 1: shadow метод
//    session, err := mgo.Dial(MONGODB_URL)
//    ...
//    // db := session.DB("xtest")           ← уровень 2: устаревшая первая попытка
//    // collection := db.C("xtest")         ← уровень 2: заменена на строку ниже
//    c := session.DB("xtest").C("xtest")   ← уровень 1: финальная версия
//}
```

`// //` здесь означает «строки, которые заменили кодом уровня 1» — это sub-alternative внутри shadow-блока. Для нас важен уровень 1: `MONGODB_URL = "ts-news-mongo:27017"` → `SHADOW_SHARES_RESOURCE`.

Масштабные **одноуровневые** блоки в Java (основная масса):

| Паттерн | Кол-во файлов | Макс. блок |
|---|---|---|
| Закомментированные `return/if/for` строки | 554 в main | — |
| Блоки ≥ 5 строк подряд с `//` | 10 файлов (без old-docs) | 21 строка (InitData.java) |
| Закомментированные методы (≥ `@Override` + тело) | 4 файла | 100+ строк (StationFoodServiceImpl) |

**Самые важные файлы:**

```
ts-station-food-service/StationFoodServiceImpl.java:  121 строка всего, 32 закомментировано
  → @Autowired + 2 полных метода закомментированы
  → содержат trainFoodRepository.findByTripId() — shadow-связь с БД

ts-common/TripResponse.java, TripInfo.java, TravelInfo.java:
  → закомментированы StringUtils.String2Date() вызовы
  → не граф, только утилиты — не создавать рёбра

ts-wait-order-service/WaitListOrder.java:
  → закомментирован equals/hashCode — не граф
```

### C.2 Алгоритм обработки многоуровневых комментариев

Для Java `//` комментарии в tree-sitter — листовые узлы. Вложенные `// //` — это **текст** внутри `line_comment`, не отдельный узел дерева.

```python
def extract_nested_comment_code(comment_text: str) -> str | None:
    """
    Принимает текст после снятия первого //
    Возвращает вложенный код, если это закомментированный код второго уровня.

    Уровень 0 (не код):     "// This is a human note"
    Уровень 1 (код):        "// return restTemplate.exchange(...)"
    Уровень 2 (// //):      "// // return x;"   → вернуть "return x;"
    Уровень 2 (////):       "////return x;"     → вернуть "return x;"
    """
    # Снять prefix // или ////
    text = comment_text.strip()
    if text.startswith('//'):
        text = text[2:].lstrip()
        if text.startswith('//'):
            text = text[2:].lstrip()
        return text if text else None
    return None

def parse_comment_node(comment_text: str, depth: int = 0) -> CommentContent:
    """
    Рекурсивный разбор вложенного комментария.
    depth: уровень вложенности (0 = первый //, 1 = второй //)
    Максимальная глубина: 2 (дальше не встречается в train-ticket)
    """
    MAX_DEPTH = 2
    if depth >= MAX_DEPTH:
        return CommentContent(text=comment_text, code=None, depth=depth)

    inner = extract_nested_comment_code(comment_text)
    if inner is None:
        return CommentContent(text=comment_text, code=None, depth=depth)

    # Проверяем: inner это код или текст?
    if is_code_fragment(inner):
        return CommentContent(
            text=comment_text,
            code=inner,
            depth=depth + 1,
        )
    else:
        # Рекурсия: может быть ещё один уровень // //
        return parse_comment_node(inner, depth + 1)
```

### C.3 Классификация по глубине

| Уровень | Паттерн | Семантика | Действие |
|---|---|---|---|
| 0 | `// текст` | Обычный комментарий | Анализировать как 7 классов ADR-020 |
| 1 | `// return x;` | Закомментированный код | Shadow pipeline: BGE-M3 классификация → ребро |
| 2 (standalone) | `// // return x;` | Дважды закомментированный, вне блока | Shadow pipeline, `conf × 0.75` |
| 2 (внутри уровня 1) | `// // db := ...` внутри `//func getNews()` | Sub-alternative — устаревшая первая попытка | Пропустить: использовать уровень 1 родителя |
| `////...` | Строка из одних слешей | Декоративный разделитель | Пропустить: `len(stripped) == stripped.count('/')` |
| `// // 阿里云...` | Нелатиница внутри `// //` | Vendor-документация | Пропустить если non-ASCII > 50% |

**Ключевое правило для ts-news-service:** Когда `// //` находится внутри уже открытого закомментированного блока (определяется по тому, что предыдущая непустая строка тоже начинается с `//`), это sub-alternative. Для shadow pipeline достаточно разобрать уровень 1 — он содержит весь необходимый контент (`MONGODB_URL`, `mgo.Dial()`).

```python
def is_decorative_divider(stripped: str) -> bool:
    """////... или ===== строки — не код."""
    unique_chars = set(stripped.replace(' ', ''))
    return len(unique_chars) <= 2 and all(c in '/-=*' for c in unique_chars)

def is_vendor_doc(text: str) -> bool:
    """Нелатиница > 50% — vendor documentation ТОЛЬКО в old-docs и вендорских JS-файлах.
    
    Важно: НЕ применять к исходному коду проекта (app.py, *.java, *.go).
    Китайские комментарии в исходнике — это авторская документация, не vendor.
    Применять только к: angular.js, jquery.js, и файлам в old-docs/.
    """
    non_ascii = sum(1 for c in text if ord(c) > 127)
    return non_ascii > len(text) * 0.5
```

**Снижение уверенности на каждый уровень вложенности:**
```python
BASE_CONF = 0.90   # для // @Value (одиночный)
DEPTH_PENALTY = 0.15  # на каждый дополнительный уровень

conf = BASE_CONF - (depth - 1) * DEPTH_PENALTY
# Уровень 1: conf = 0.90
# Уровень 2: conf = 0.75
# Уровень 3: conf = 0.60 → ниже порога → пропустить
```

### C.4 JS: `// //` в ts-ui-dashboard

В `travel.js` встречается JS-специфичный паттерн — закомментированный объект внутри закомментированного вызова:

```javascript
// $("#travel_create_button").click(function(){
//     var travelCreateInfo = new Object();
// //     var travelCreateInfo = {          ← уровень 2
// //         tripId : "..."                 ← уровень 2
// //     }
//     travelCreateInfo.tripId = ...
```

Это **устаревший вариант кода** (объектный литерал заменён на `new Object()`). Для анализа ts-ui-dashboard:
- Уровень 1 → `shadow pipeline` для URL в `$.ajax({url: "/travel/create"})` → RouteIndex hint
- Уровень 2 → игнорировать (это замена синтаксиса, не зависимость)

### C.5 Блок-комментарии `/* */`: не вложимы

В Java `/* */` не могут быть вложены синтаксически (`/* /* */` → ошибка компиляции).  
В train-ticket `/* // */` конструкция не встречается.  
Правило: если в text `/* ... */` встречается `//` — это просто текст внутри блока, не вложенный комментарий.

---

## D. Итоговые правила для реализации

```python
# 1. Исключение файлов
SKIP_PATTERNS = ["src/test", "node_modules", "old-docs", "target", "__pycache__"]

# 2. Тестовые URL как подтверждение (не первичный источник)
TEST_URL_PATTERN = re.compile(r'MockMvcRequestBuilders\.\w+\("(/[^"]+)"')

# 3. Shadow code с глубиной вложенности
def shadow_conf(base: float, depth: int) -> float:
    penalty = 0.15
    return max(0.0, base - (depth - 1) * penalty)

# 4. Пороги
SHADOW_SINGLE_CONF = 0.90    # // @Value
SHADOW_DOUBLE_CONF = 0.75    # // // @Value
SHADOW_TRIPLE_CONF = 0.60    # // // // — ниже порога 0.65, не создавать ребро

# 5. Тест-специфичные паттерны — никогда не создавать ребро
TEST_SKIP_PATTERNS = [
    r'Mockito\.when\(',
    r'@Mock\b',
    r'@MockBean',
    r'\.thenReturn\(',
    r'InjectMocks',
]
```

**Таблица: что делаем с каждым типом файла**

| Файл | Граф | RouteIndex | Shadow | Примечание |
|---|---|---|---|---|
| `src/main/**/*.java` | ✅ | ✅ | ✅ | Основной источник |
| `src/test/**/*.java` | ❌ | hint only | ❌ | MockMvc URL → подтверждение |
| `node_modules/**` | ❌ | ❌ | ❌ | NPM-зависимости |
| `old-docs/**` | ❌ | ❌ | ❌ | Архив |
| `target/**` | ❌ | ❌ | ❌ | Build artifacts |

---

## E. TestAdapter: полное исследование гипотез и Two-Phase стратегия

### E.1 Исследованные гипотезы (train-ticket, 103 тест-файла, 40 сервисов с тестами)

| # | Гипотеза | Результат | Ценность |
|---|---|---|---|
| H1 | Пути до сервисов в именах файлов/директорий | ✅ Подтверждена | Средняя — уже есть в FileWalker |
| H2 | `@MockBean` → явные декларации зависимостей | ❌ Не используется | Ноль — в train-ticket нет ни одного `@MockBean` |
| H3 | Хардкод URL в `Mockito.when(restTemplate.exchange("http://ts-X:PORT/path"))` | ✅ Подтверждена | **Высокая** — 17 сервисов, точные порты |
| H4 | Граф caller→callee из тест-файлов | ✅ Подтверждена | **Высокая** — 30+ пар без парсинга main-кода |
| H5 | MockMvc `perform(get/post("/api/v1/..."))` → API-контракт | ✅ Подтверждена | **Высокая** — 186 уникальных путей с HTTP-методами |
| H6 | RabbitMQ очереди в тестах → имена очередей | ❌ Не подтверждена | Нет — только `Queues.queueName` (статическая константа, не раскрывает имя) |
| H7 | `test/resources/application.properties` → конфиги | ❌ Пусто | Нет — 0 test/resources файлов |
| H8 | `@SpringBootTest` → окружение и порты | ⚠️ Частично | Минимум — 1 пустой `contextLoads()` в ts-wait-order-service |

**9 сервисов без тестов** (белые пятна): `ts-avatar-service`, `ts-gateway-service`, `ts-delivery-service`, `ts-news-service`, `ts-ticket-office-service`, `ts-train-food-service`, `ts-ui-dashboard`, `ts-voucher-service`, `ts-common`.

---

### E.2 Карта сервис→порт из мок-URL (train-ticket, высокая достоверность)

Тесты содержат хардкод URL вида `"http://ts-X:PORT/api/v1/..."`. Это надёжнее docker-compose, потому что написано разработчиком явно для каждого вызова.

| Сервис | Порт | Источник |
|---|---|---|
| `ts-order-service` | 12031 | 17 мок-вызовов |
| `ts-order-other-service` | 12032 | 11 мок-вызовов |
| `ts-station-service` | 12345 | 10 мок-вызовов |
| `ts-travel-service` | 12346 | 6 мок-вызовов |
| `ts-travel2-service` | 16346 | 6 мок-вызовов |
| `ts-train-service` | 14567 | 6 мок-вызовов |
| `ts-user-service` | 12342 | 7 мок-вызовов |
| `ts-price-service` | 16579 | 5 мок-вызовов |
| `ts-route-service` | 11178 | 4 мок-вызовов |
| `ts-contacts-service` | 12347 | 4 мок-вызовов |
| `ts-config-service` | 15679 | 4 мок-вызовов |
| `ts-seat-service` | 18898 | 3 мок-вызовов |
| `ts-notification-service` | 17853 | 3 мок-вызовов |
| `ts-consign-price-service` | 16110 | 2 мок-вызовов |
| `ts-auth-service` | 12340 | 2 мок-вызовов |
| `ts-inside-payment-service` | 18673 | 1 мок-вызов |
| `ts-verification-code-service` | 15678 | 1 мок-вызов |

---

### E.3 Граф caller→callee из тестов

Каждый сервис с тестами мокирует только те сервисы, которые реально вызывает. Это граф `CALLS_SERVICE` рёбер с `source=test_mock`, `confidence=0.85`.

```
ts-admin-basic-info-service → ts-config-service, ts-contacts-service, ts-price-service, ts-station-service, ts-train-service
ts-admin-order-service      → ts-order-other-service, ts-order-service
ts-admin-route-service      → ts-route-service
ts-admin-travel-service     → ts-travel-service, ts-travel2-service
ts-admin-user-service       → ts-user-service
ts-auth-service             → ts-verification-code-service
ts-basic-service            → ts-price-service, ts-route-service, ts-station-service, ts-train-service
ts-cancel-service           → ts-inside-payment-service, ts-notification-service, ts-order-other-service, ts-order-service, ts-user-service
ts-consign-service          → ts-consign-price-service
ts-execute-service          → ts-order-other-service, ts-order-service
ts-inside-payment-service   → ts-order-service
ts-order-other-service      → ts-station-service
ts-order-service            → ts-station-service
ts-preserve-other-service   → ts-notification-service, ts-seat-service, ts-user-service
ts-preserve-service         → ts-notification-service, ts-seat-service, ts-user-service
ts-rebook-service           → ts-seat-service
ts-user-service             → ts-auth-service
```

**Важно:** это `CALLS_SERVICE` рёбра уровня «сервис→сервис», не метод→метод. Для граней метода нужен Value Flow Graph из main-кода.

---

### E.4 API-контракты из MockMvc (186 путей)

`mockMvc.perform(get/post/put/delete("/api/v1/..."))` → тест проверяет реальный endpoint.

**Применение:** если путь есть в тесте, но нет в `@RequestMapping` main-кода → `TECH_DEBT[ORPHAN_TEST_URL]`.  
**Приоритет:** `gateway.yml` > `@RequestMapping` > MockMvc-hint.

```python
# Паттерн для MockMvc (оба варианта синтаксиса)
_MOCK_MVC_RE = re.compile(
    r'\.perform\(\s*(?:MockMvcRequestBuilders\.)?'
    r'(get|post|put|delete|patch)\s*\(\s*"(/[^"]+)"'
)
```

---

### E.5 TestAdapter: Two-Phase реализация

```
Phase 1 — Locate (ripgrep, если доступен):
  rg --files-with-matches -e "restTemplate.exchange" -e "\.perform\(" \
     --glob "*Test*.java" <target>
  Fallback: rglob("**/test/**/*Test*.java")

Phase 2 — Extract (stdlib regex per file):
  pattern A: Mockito.when(restTemplate.exchange("http://ts-X:PORT/path", HttpMethod.Y
           → Finding(CALLS_SERVICE, conf=0.85, data={callee, port, path, http_method, source="test_mock"})

  pattern B: .perform(get/post/put/delete("/api/v1/path"))
           → Finding(HTTP_ENDPOINT, conf=0.90, data={method, path, source="test_hint"})
```

**Caller определяется из пути файла:** первый сегмент вида `ts-*` в пути к тест-файлу.

---

### E.6 Обновлённые правила (дополнение к D)

```python
# Мок-URL из тестов → CALLS_SERVICE с пометкой (не ребро основного графа!)
TEST_MOCK_URL_RE = re.compile(
    r'Mockito\.when\(\s*restTemplate\.exchange\(\s*'
    r'"(http://ts-[a-zA-Z0-9-]+:\d+)([^"]*)"'
)
# HttpMethod на следующей строке
TEST_HTTP_METHOD_RE = re.compile(r'HttpMethod\.([A-Z]+)')

# Достоверность test_mock ниже main-кода
TEST_MOCK_CONF  = 0.85   # caller→callee на уровне сервиса
TEST_HINT_CONF  = 0.90   # MockMvc path → HTTP_ENDPOINT подтверждение

# Никогда не создавать из тестов:
# - METHOD-level CALLS_SERVICE (не знаем какой метод вызывает)
# - SHARES_RESOURCE (нет БД-URL в тестах)
# - ASYNC_CALL (Queues.queueName не раскрывает имя очереди)
```

---

## F. Inline документация как источник метаданных

### F.1 SecurityConfig → auth-метаданные HTTP_ENDPOINT

Каждый Java-сервис имеет `SecurityConfig.java` (39 файлов в train-ticket).  
Извлекаемые паттерны:

```java
// Паттерны доступа (обрабатываем)
.antMatchers("/api/v1/configservice/**").permitAll()                    → auth_required=[]
.antMatchers("/api/v1/adminuserservice/users/**").hasRole("ADMIN")       → auth_required=["ADMIN"]
.antMatchers("/api/v1/orderservice/**").hasAnyRole("ADMIN","USER")       → auth_required=["ADMIN","USER"]
.antMatchers(HttpMethod.POST, "/api/v1/configservice/configs").hasAnyRole(admin) → {method=POST, auth=["ADMIN"]}

// НЕ обрабатываем (шум, одинаковы во всех 39 сервисах)
.antMatchers("/swagger-ui.html", "/webjars/**", "/images/**",
             "/configuration/**", "/swagger-resources/**", "/v2/**").permitAll()

// security metadata сервиса (одинаковы везде — просто помечаем)
.csrf().disable()
.httpBasic().disable()
.sessionManagement().sessionCreationPolicy(SessionCreationPolicy.STATELESS)
```

`antMatchers` → обогащение `HTTP_ENDPOINT` finding из `@RequestMapping`:  
`RouteIndex[path].auth_required = parsed_roles`.

**Где реализовать:** Java-парсер при обнаружении класса с `WebSecurityConfigurerAdapter` — вызвать `SecurityConfigExtractor`. Или semgrep-правило для portability. DEVPLAN задача 14b.

---

### F.2 Структурированные logger → `doc_summary`

Формат в train-ticket (67+ вызовов в одном `BasicServiceImpl`): `[CLASS][METHOD][DESCRIPTION]`

```java
LOGGER.info("[queryForTravel][all done][result: {}]", result)
LOGGER.warn("[queryForStationId][Query for stationId error][stationName: {}, message: {}]", ...)
```

Регекс: `\[([^\]]+)\]\[([^\]]+)\]` → `(method_name, description)`

- Первый bracket: имя метода (совпадает с реальным именем → легко соотнести с узлом)
- Второй bracket: описание действия → `doc_summary` поля NodeMeta

Достоверность: 0.8 (не 1.0 — разработчик мог написать неактуальное описание).

---

### F.3 Python `print()` как logger

В Python-сервисах `print()` выполняет ту же роль что `logger.info()` в Java.  
Паттерны в train-ticket:

```python
print("人脸数：", len(faces), "\n")        # face_detect.py — данные с китайским текстом
print("[DOCKER HUB LOGIN] login username:{}")  # структурированный, как logger
print("[FAIL]" + image_name + " build failed")  # event log
print(mysql_config)                             # CONFIG_VALUE — раскрывает структуру
```

Правило: `print(...)` в топ-уровне функции или модуля → извлечь в `doc_summary` если строка-литерал. Если `print(var)` — игнорировать (runtime значение).

---

### F.4 Китайские комментарии → не игнорировать

**Неверная политика:** `is_vendor_doc()` отфильтровывает китайский текст > 50%.  
Применять только к: `angular.js`, `jquery.js`, и `old-docs/` — там vendor-документация.

Для исходного кода проекта (`app.py`, `*.java`, `*.go`):
- BGE-M3 поддерживает 100+ языков — Chinese вектор будет семантически корректным
- `~~text~~` = завершено (strikethrough Markdown в комментарии)
- `~text` = в процессе
- `# N. text` без маркера = TODO → `TECH_DEBT[ORPHAN_TODO]`

```python
# app.py:
# TODO:
# ~~1. 获取图片~~         → done (strikethrough)
# ~2. 检测图片是否ok      → in-progress
# ~3. 人脸检测&切割       → in-progress
# ~4. 返回base64格式的图片 → in-progress
# 5. 前端传文件           → TECH_DEBT: открытая задача
# 6. Dockerfile部署       → TECH_DEBT: открытая задача
```

---

### F.5 `app.run(host="0.0.0.0", port=N)` → SERVICE_DEF

`0.0.0.0` означает «слушать на всех интерфейсах» — это порт сервиса.  
**Не является хостом** — нельзя использовать как `host` в `ParsedURL`.

```python
# Паттерн для Python/Flask + аналоги
_APP_RUN_RE = re.compile(
    r'(?:app|server|socketio)\.run\('
    r'[^)]*'
    r'port\s*=\s*(\d+)'
)

# 0.0.0.0 нормализация
def normalize_listen_host(host: str, service_name: str) -> str:
    if host in ("0.0.0.0", "::"):   # IPv4/IPv6 all interfaces
        return service_name          # нормализуем в имя сервиса
    return host
```

**Приоритет источников порта для Python-сервисов:**
1. `app.run(port=N)` — самый авторитетный (разработчик написал явно)
2. Dockerfile `EXPOSE N`
3. docker-compose `ports: ["HOST:N"]`
4. Тест-URL (если есть тесты)
