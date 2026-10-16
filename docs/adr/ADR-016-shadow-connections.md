# ADR-016: Теневые связи (закомментированный код)

**Статус:** Принято  
**Дата:** 2026-10-13  
**Связанные:** ADR-001, ADR-002, ADR-003, ADR-014

## Контекст

В `train-ticket` найдены значительные блоки закомментированного кода с архитектурными зависимостями:

- **ts-news-service (Go)**: весь MongoDB-драйвер `mgo` закомментирован — сервис изначально обращался к `ts-news-mongo:27017`, но зависимость убрана без удаления кода.
- **ts-station-food-service (Java)**: методы `createTrainFood`, `listTrainFood`, `listTrainFoodByTripId` полностью закомментированы вместе с `@Autowired TrainFoodRepository`.
- **ts-order-service, ts-order-other-service, ts-admin-user-service**: закомментированы `@Value("${...service.url}")` — было прямое конфигурирование, убрано при переходе на service discovery.
- **ts-rebook-service, ts-preserve-service, ts-travel-service**: от 18 до 20 закомментированных строк каждый.

Эти связи **существовали**, **могут быть восстановлены** (одно раскомментирование) и представляют реальный архитектурный риск при blast radius анализе: изменение зависимой библиотеки/сервиса может вернуть эти связи к жизни.

---

## Решение

### Уровень 1: Детекция закомментированных блоков (tree-sitter)

Для каждого языка tree-sitter выдаёт AST с явными comment-узлами. Закомментированные блоки кода — это comment-узлы с содержимым похожим на код.

**Java (tree-sitter-java):**
```python
# Запрос находит однострочные и многострочные комментарии
JAVA_COMMENT_QUERY = """
(line_comment) @line_comment
(block_comment) @block_comment
"""
```

**Python (tree-sitter-python):**
```python
PYTHON_COMMENT_QUERY = """
(comment) @comment
"""
```

**Go (tree-sitter-go):**
```python
GO_COMMENT_QUERY = """
(comment) @comment
"""
```

Извлечённый текст передаётся в классификатор.

---

### Уровень 2: Классификация содержимого (лёгкий embedding-классификатор)

**Задача:** Отличить закомментированный код от обычного текстового комментария.

**Модель:** BGE-M3 (уже используется в ADR-014 для env var классификации). Никаких дополнительных моделей.

**Few-shot примеры (встроенные в `src/infrastructure/seeds/shadow_patterns.yaml`):**

```yaml
code_examples:
  - text: "// restTemplate.exchange(getServiceUrl(\"ts-station-service\") + \"/api/...\", ...)"
    label: code
  - text: "// trainFoodRepository.findById(tf.getId())"
    label: code
  - text: "//import \"labix.org/v2/mgo\""
    label: code
  - text: "// @Autowired"
    label: code
  - text: "// @Value(\"${station-service.url}\")"
    label: code
  - text: "// session, err := mgo.Dial(MONGODB_URL)"
    label: code

text_examples:
  - text: "// This method handles the order creation flow"
    label: text
  - text: "// TODO: add validation"
    label: text
  - text: "// author fdse"
    label: text
  - text: "// Congratulations: Your News Service Complete"
    label: text
```

**Порог:** cosine similarity ≥ 0.75 к ближайшему code-примеру → `is_code = True`.

---

### Уровень 3: Извлечение зависимостей из закомментированного кода

Если `is_code = True`, закомментированный блок отправляется на **повторный мини-парсинг**:

```python
def extract_shadow_dependencies(comment_text: str, host_service: str) -> list[ShadowEdge]:
    """Парсим закомментированный код как обычный, но помечаем результат shadow=True."""
    # Убираем префиксы // и /* */
    clean_code = strip_comment_markers(comment_text)
    # Попытка прогнать через тот же парсер что и обычный код
    # Если парсинг частичный — тоже валидно (PARTIAL_PARSED shadow)
    dependencies = try_parse_dependencies(clean_code, language=host_service_language)
    return [ShadowEdge(dep, shadow=True, confidence=classifier_score) for dep in dependencies]
```

**Важно:** Ошибки парсинга закомментированного кода — **не ошибки**: код мог быть не полностью валидным синтаксически. Любой частичный результат принимается.

---

### Уровень 4: Shadow-рёбра в графе

Теневые связи хранятся как обычные рёбра, но с флагом в метаданных:

```python
# Новые edge_type значения с префиксом SHADOW_
EdgeType.SHADOW_CALLS          # закомментированный вызов метода
EdgeType.SHADOW_CALLS_SERVICE  # закомментированный вызов сервиса
EdgeType.SHADOW_USES_DTO       # закомментированное использование DTO
EdgeType.SHADOW_SHARES_RESOURCE # закомментированная зависимость ресурса (DB, queue)
```

```python
@dataclass
class Edge:
    source_id: NodeId
    target_id: NodeId
    edge_type: str
    weight: float
    metadata: dict

# Пример shadow-ребра
shadow_edge = Edge(
    source_id="ts-news-service::main.main",
    target_id="ts-news-mongo::mongo#connection",
    edge_type=EdgeType.SHADOW_SHARES_RESOURCE,
    weight=0.4,  # сниженный вес по сравнению с активной связью (1.0)
    metadata={
        "shadow": True,
        "shadow_confidence": 0.87,
        "comment_location": "main.go:5-12",
        "reason": "commented_out_import",
        "original_text": "//import \"labix.org/v2/mgo\""
    }
)
```

**Схема PostgreSQL — дополнение к ADR-008:**
```sql
-- В таблице edges (ADR-008) поле metadata уже есть
-- shadow-рёбра хранятся в том же edges, различаются edge_type и metadata.shadow

-- Индекс для фильтрации shadow
CREATE INDEX ON edges ((metadata->>'shadow'));
```

---

### Уровень 5: Учёт в blast radius обходе

По умолчанию shadow-рёбра **включаются в обход** с пониженным весом (0.4 вместо 1.0):

```python
# BFS в ADR-005 — параметр конфигурации
@dataclass
class TraversalConfig:
    max_depth: int = 6
    include_shadow: bool = True     # включать ли теневые рёбра
    shadow_weight_factor: float = 0.4  # множитель веса для shadow-рёбер
```

**Обоснование включения по умолчанию:** При изменении зависимой библиотеки/сервиса закомментированный код — ближайший кандидат к восстановлению. Blast radius должен это отражать.

**Исключение:** Явный флаг `include_shadow=False` в API-запросе позволяет получить только активные зависимости.

---

## Примеры из train-ticket

### ts-news-service (Go) — MongoDB shadow connection

```go
// Закомментированные импорты:
//"labix.org/v2/mgo"
//"labix.org/v2/mgo/bson"

// Закомментированная константа:
//const (
//    MONGODB_URL = "ts-news-mongo:27017"
//)

// Закомментированная функция getNews — полная реализация
```

→ Shadow ребро: `ts-news-service` → `ts-news-mongo` (тип `SHADOW_SHARES_RESOURCE`)  
→ Означает: **сервис ts-news-mongo когда-то существовал как зависимость**

### ts-station-food-service (Java) — закомментированные методы + repository

```java
//    @Autowired
//    TrainFoodRepository trainFoodRepository;
//    @Override
//    public TrainFood createTrainFood(TrainFood tf, HttpHeaders headers) { ... }
```

→ Shadow ребро: `ts-station-food-service::StationFoodServiceImpl` → `TrainFoodRepository` (тип `SHADOW_CALLS`)

### ts-order-service (Java) — закомментированный @Value

```java
//    @Value("${station-service.url}")
```

→ Shadow ребро: `ts-order-service` → `ts-station-service` (тип `SHADOW_CALLS_SERVICE`)  
→ Говорит о том, что прямой URL-конфиг был заменён на service discovery

---

## Особые случаи

### Многоязычные комментарии
- Комментарии на русском и английском: анализируются.  
- Иероглифы и другие языки (пример: TODO на китайском в `ts-avatar-service`): **игнорируются** для классификации (не код), но не вызывают ошибки.

### Вложенные комментарии
- `/* // закомментированный код */` — берём внутреннее содержимое.  
- Javadoc `/** ... */` — не анализируется на shadow (только документация).

### Минимальный размер блока
- Менее 2 строк → не классифицируем как shadow-код (слишком коротко для осмысленного извлечения зависимостей).
- Одна строка `// restTemplate.exchange(...)` — исключение: явная зависимость.

---

## Как отображается в UI (ADR-012)

Shadow-рёбра в визуализации:
- **Пунктирная линия** вместо сплошной.
- Цвет: серый (`#9E9E9E`) вместо основного цвета по типу ребра.
- Tooltip: `"Теневая связь: закомментированный код (confidence: 87%)"`.
- Скрыты по умолчанию на уровне LOD 3 (низкая детализация), видны на LOD 1-2.

---

## Последствия

- Blast radius незначительно расширяется из-за теневых рёбер (ожидаемо).
- Классификатор BGE-M3 уже используется — нет новых моделей.
- Парсинг закомментированного кода может давать `PARTIAL_PARSED` — это нормально, не ошибка.
- При IoU-оценке теневые рёбра могут снизить precision, но повысить recall. Флаг `include_shadow=False` позволяет сравнивать оба режима.
- `shadow_patterns.yaml` пополняется так же как `env_patterns.yaml` (ADR-014).
