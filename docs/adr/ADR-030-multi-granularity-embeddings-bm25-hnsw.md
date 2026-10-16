# ADR-030: Многогранулярные эмбеддинги, BM25/гибридный поиск, HNSW и Map-Reduce для сервисов

**Статус:** Принято (A–C: Tier 3; D Map-Reduce: Tier 4; E ColBERT hot-nodes: Tier 5)  
**Дата:** 2026-10-14  
**Связанные:** ADR-014 (ML-пайплайн), ADR-020 (контекстные эмбеддинги), ADR-022 (embedding roles), ADR-023 (SymbolIndex), ADR-027 (FileSimilarityIndex)

---

## Контекст

ADR-020 и ADR-022 определяют **роли** эмбеддингов (ENV_PATTERN, CODE_STRUCTURE, SEMANTIC_SEARCH…).  
Этот ADR отвечает на вопрос: **как именно хранить и искать** — сколько векторов на узел, какой индекс, как строить сводные представления над кодом.

Три взаимосвязанные проблемы:

1. **Один вектор на узел** плохо покрывает все типы запросов: «структурно похожие классы», «код который делает то же самое» и «дай LLM полный контекст» — разные задачи с разными оптимальными входами.
2. **Dense-only поиск** проигрывает на точных идентификаторах: `OrderServiceImpl`, `QUEUE_EMAIL_SERVICE`, `getServiceUrl` — BM25 по словам даёт recall ~100%, dense — ~70%.
3. **Нет сервисного уровня**: поиск по файлам при запросе «какие сервисы обрабатывают платежи?» засоряет топ-K файлами одного сервиса.

---

## A. Три гранулярности на узел

### Принцип

Каждый узел хранит три эмбеддинга с разным текстовым входом:

```
emb_structural  ← минимальный структурный шаблон
emb_semantic    ← ролевой шаблон + аннотации + сигнатуры
emb_full        ← полный контекст для LLM
```

### Входные шаблоны

#### `emb_structural` (~150 токенов) — для FileSimilarityIndex (ADR-027)

Только скелет без реализации. Тела методов не включаются. Цель — найти «структурно похожие» классы в других сервисах.

```
[LANG:java] [ANN:@Service,@Transactional]
CLASS:PreserveServiceImpl
  IMPL:PreserveService
  METHODS: preserve(PreserveDto) void
           queryForPreserve(PreserveDto) Result
           getPreserveByAccountId(String) List<Preserve>
  FIELDS: preserveRepository PreserveRepository
          trainFoodServiceProxy TrainFoodServiceProxy
```

#### `emb_semantic` (~400 токенов) — для ConceptRegistry (ADR-022) и bi-encoder реранкера (ADR-023)

Роль + аннотации + первые 80 токенов каждого метода. Отвечает на «что делает этот узел».

```
[SERVICE:ts-preserve-service][PKG:preserve.service]
[ROLE:business_logic][ANN:@Service,@Transactional]
[CALLS:ts-train-service,ts-food-service,ts-order-service]
METHOD preserve(dto): check availability → create Preserve → notify
METHOD queryForPreserve(dto): filter by dates/seatType → return list
METHOD getPreserveByAccountId(id): findByAccountId → map results
```

#### `emb_full` (~1500 токенов) — для LLM context window (ADR-020)

Полный текст + граф-соседи. Используется как фрагмент контекста для prompt.

```
[FULL_CONTEXT] ts-preserve-service/PreserveServiceImpl.java
<полный текст файла или до max_tokens>
[OUTGOING_EDGES]
  CALLS_SERVICE: ts-train-service (/api/v1/train/query)
  CALLS_SERVICE: ts-food-service (/api/v1/food/query)
  USES_DTO: PreserveDto, Preserve
```

### Схема LanceDB

```python
import pyarrow as pa

VECTOR_DIM = 1024  # BGE-M3

node_schema = pa.schema([
    pa.field("node_id",       pa.string()),
    pa.field("service",       pa.string()),
    pa.field("file",          pa.string()),
    pa.field("node_type",     pa.string()),       # CLASS / METHOD / SERVICE / ...
    # Три вектора
    pa.field("emb_struct",    pa.list_(pa.float32(), VECTOR_DIM)),
    pa.field("emb_semantic",  pa.list_(pa.float32(), VECTOR_DIM)),
    pa.field("emb_full",      pa.list_(pa.float32(), VECTOR_DIM)),
    # Sparse (BM25-подобный от BGE-M3)
    pa.field("sparse_indices", pa.list_(pa.int32())),
    pa.field("sparse_values",  pa.list_(pa.float32())),
    # Rich metadata (JSON string для гибкости)
    pa.field("metadata",      pa.string()),
])
```

### Выбор вектора по задаче

```python
TASK_TO_EMBEDDING: dict[str, str] = {
    "sibling_search":      "emb_struct",    # ADR-027 FileSimilarityIndex
    "concept_match":       "emb_semantic",  # ConceptRegistry fallback
    "reranker":            "emb_semantic",  # bi-encoder реранкер (ADR-023 §H)
    "llm_context":         "emb_full",      # context window assembly
    "service_query":       "emb_semantic",  # после Map-Reduce агрегации (§D)
    "env_classification":  "emb_semantic",  # ADR-014 Component A
}
```

BGE-M3 возвращает все три типа в **одном инференсе** — `dense_vecs`, `sparse_vecs`, `colbert_vecs` — дополнительных вызовов модели не требуется.

---

## B. Роль BM25 и гибридный поиск

### Когда dense-поиск проигрывает

| Запрос | Dense (BGE-M3) | BM25 |
|---|---|---|
| `"getServiceUrl"` | ~0.71 (семантическое) | ~1.0 (точное слово) |
| `"QUEUE_EMAIL_SERVICE"` | ~0.68 | ~1.0 |
| `"@FeignClient name=ts-order"` | ~0.73 | ~1.0 |
| `"что делает order processing"` | ~0.89 | ~0.30 |
| `"сервис похожий на OrderService"` | ~0.84 | ~0.15 |

**BM25 обязателен для**: точных имён классов, методов, ENV-переменных, аннотаций, error-кодов.  
**Dense обязателен для**: семантических запросов, кросс-языкового поиска, концептуального матчинга.

### Источник BM25 в системе

BGE-M3 генерирует **learned sparse vectors** (аналог SPLADE): числа по индексам словарного токена, уже улучшенные по сравнению с классическим TF-IDF. Хранятся в LanceDB как разреженные пары `(indices, values)`.

```python
# Вызов BGE-M3: все три вектора за один проход
from FlagEmbedding import BGEM3FlagModel

model = BGEM3FlagModel("BAAI/bge-m3", use_fp16=True)

outputs = model.encode(
    texts,
    return_dense=True,
    return_sparse=True,
    return_colbert_vecs=False,  # только для Tier 5 hot-nodes
    batch_size=8,
    max_length=512,
)

dense   = outputs["dense_vecs"]        # np.ndarray (N, 1024)
sparse  = outputs["lexical_weights"]   # list[dict[int, float]]
```

### Гибридный scoring

```python
def hybrid_score(
    dense_score: float,    # cosine(query_dense, doc_dense)
    sparse_score: float,   # dot(query_sparse, doc_sparse)
    alpha: float = 0.7,    # вес dense
) -> float:
    return alpha * dense_score + (1 - alpha) * sparse_score
```

`alpha = 0.7` — эмпирически: акцент на семантику, но BM25 вытаскивает точные совпадения.  
Для запросов по идентификаторам (метод, класс) `alpha = 0.3`.

### Детектор типа запроса

```python
def _is_identifier_query(query: str) -> bool:
    """Запрос — это имя символа, а не описание на естественном языке."""
    import re
    # camelCase, UPPER_CASE, FQN с точкой
    return bool(re.match(r'^[A-Z_]{3,}$|^[a-z][a-zA-Z0-9]{2,}$|.*\.[A-Z]', query.strip()))

alpha = 0.3 if _is_identifier_query(query) else 0.7
```

---

## C. HNSW: индекс и его место в системе

### Что такое HNSW в контексте этого проекта

HNSW (Hierarchical Navigable Small World) — индекс для approximate nearest neighbor (ANN) поиска по dense-векторам.  
**Не заменяет BFS по графу зависимостей** — отвечает на другой вопрос:

| Механизм | Вопрос | Входные данные |
|---|---|---|
| BFS по NetworkX | «к чему подключён этот код?» | структурные рёбра CALLS, USES_DTO |
| HNSW по LanceDB | «какой код похож на этот?» | dense-векторы (emb_struct, emb_semantic) |

Оба механизма дополняют друг друга — HNSW находит узлы без явных рёбер в графе.

### Когда HNSW нужен vs не нужен

| Размер индекса | Стратегия | Обоснование |
|---|---|---|
| < 1 000 узлов (один train-ticket) | **Brute-force cosine** | HNSW-overhead не окупается; numpy быстрее |
| 1 000 – 50 000 узлов (средний монорепо) | **HNSW (LanceDB default)** | sub-linear поиск, recall ~95% при ef=64 |
| > 50 000 узлов (очень большой монорепо) | **IVF-PQ + HNSW** | HNSW потребляет ~100 байт/вектор; при 100k узлах ×3 вектора = 1.2GB RAM |

LanceDB автоматически строит HNSW при `num_rows > 1000` — никакого ручного управления.

```python
# LanceDB создаёт HNSW автоматически при первом create_index
tbl.create_index(
    metric="cosine",
    vector_column_name="emb_semantic",
    index_cache_size=256,  # MB
    num_partitions=8,      # для IVF при > 50k
    num_sub_vectors=64,    # PQ-квантование
)
```

### HNSW над структурой проекта: роль в blast radius

HNSW-поиск по `emb_struct` реализует **FileSimilarityIndex** (ADR-027):

```
Изменился: ts-notification-service/NotificationServiceImpl.java
→ HNSW.search(emb_struct, top_k=10, min_sim=0.85)
→ Находит: ts-consign-service/ConsignNotificationService.java (sim=0.91)
            ts-inside-payment-service/PaymentNotifier.java   (sim=0.87)
→ Эти файлы добавляются в blast radius через SIBLING_OF рёбра
```

Это связи которых **нет в графе явных зависимостей** — только через структурное сходство.

### Параметры для train-ticket (~600 файлов × 3 вектора)

```python
HNSW_CONFIG = {
    "ef_construction": 128,   # качество построения (выше = точнее, медленнее)
    "M": 16,                   # число соседей в графе HNSW
    "ef_search": 64,           # качество поиска (runtime)
    "expected_recall": 0.95,   # цель
}
```

RAM: 600 узлов × 3 вектора × 1024 dims × 4 bytes = ~7MB — brute-force быстрее HNSW. HNSW реально нужен при анализе нескольких репо или при инкрементальном наращивании индекса.

---

## D. Map-Reduce: сервисные векторы (Tier 4)

### Проблема без агрегации

Запрос «сервис похожий на ts-preserve-service» без агрегации даёт top-K файлов, а не сервисов. 70% результатов — варианты из одного сервиса.

### Map-фаза

```python
# уже есть: emb_semantic для каждого файла
file_embeddings: dict[str, np.ndarray]  # node_id → vector
```

### Reduce-фаза: взвешенное mean-pooling

```python
import numpy as np
import networkx as nx

def reduce_service_embedding(
    G: nx.DiGraph,
    service_name: str,
    file_embeddings: dict[str, np.ndarray],
) -> np.ndarray:
    # Узлы сервиса
    nodes = [n for n, d in G.nodes(data=True) if d.get("service") == service_name]
    
    # Веса = PageRank (централизованность в import-графе)
    pagerank = nx.pagerank(G, alpha=0.85)
    
    vectors, weights = [], []
    for node in nodes:
        if node in file_embeddings:
            vectors.append(file_embeddings[node])
            weights.append(pagerank.get(node, 1e-6))
    
    if not vectors:
        return np.zeros(1024, dtype=np.float32)
    
    arr = np.stack(vectors)
    w = np.array(weights) / np.sum(weights)
    return (arr * w[:, None]).sum(axis=0).astype(np.float32)
```

### Хранение сервисного вектора

```python
# Отдельная таблица для быстрого cross-service поиска
service_schema = pa.schema([
    pa.field("service",         pa.string()),
    pa.field("emb_service",     pa.list_(pa.float32(), 1024)),
    pa.field("file_count",      pa.int32()),
    pa.field("tech_hints",      pa.list_(pa.string())),  # ["db:mysql","mq:rabbitmq"]
    pa.field("api_paths",       pa.list_(pa.string())),  # ["/api/v1/preserve/..."]
    pa.field("top_annotations", pa.list_(pa.string())),  # ["@Service","@RestController"]
])
```

### Двухфазный векторный поиск

```
Запрос: "найди сервисы, похожие на ts-preserve-service"

Фаза 1: service_table.search(emb_service, top_k=5)
        → [ts-consign-service(0.91), ts-ticket-service(0.83), ...]

Фаза 2: для каждого кандидата: node_table.search(emb_semantic, filter=service, top_k=3)
        → конкретные файлы/методы для LLM-контекста
```

---

## E. Multi-vector ColBERT (Tier 5 — только для hot-nodes)

ColBERT хранит вектор на каждый токен (~512 векторов вместо 1). Поиск = MaxSim по всем парам.

| Метрика | Dense (1 vec) | ColBERT (512 vecs) |
|---|---|---|
| Recall@10 | ~0.87 | ~0.95 |
| RAM (1k узлов) | 4MB | 2GB |
| Latency | 2ms | 80ms |

**Стратегия**: ColBERT только для узлов с `pagerank > 0.01` или `in_degree > 10` (gateway, common-library, shared DTO).

```python
def should_use_colbert(node: dict) -> bool:
    return (
        node.get("pagerank", 0) > 0.01
        or node.get("in_degree", 0) > 10
        or node.get("node_type") in ("INTERFACE", "DTO")
    )
```

---

## F. Метаданные узлов без Ollama

Олама нужна только для текстовых описаний (`doc_summary`). Всё остальное извлекается детерминированно:

| Поле | Источник | Tier |
|---|---|---|
| `annotations` | tree-sitter / semgrep | 1 |
| `framework` | Framework Profile (ADR-022) | 2 |
| `tech_hints` | `DepsAdapter` (pom.xml, requirements.txt, ...) | 1 |
| `api_paths` | `@RequestMapping` + RouteIndex (ADR-011) | 2 |
| `in_degree`, `out_degree` | NetworkX | 1 |
| `pagerank` | `nx.pagerank(G)` | 2 |
| `scc_id` | `nx.strongly_connected_components(G)` | 3 |
| `has_tests` | `find *Test.java *_test.py` по паттерну | 1 |
| `churn_score` | `git log --follow --format=%H -- <file>` → count/total | 3 |
| `doc_summary` | `@ApiOperation`, JSDoc, Javadoc первый параграф | 2 |
| `doc_summary` (fallback) | Ollama summarize(emb_full_text) | 4 |

```python
@dataclass
class NodeMeta:
    # Структурное
    annotations:      list[str]    # ["@RestController", "@Transactional"]
    framework:        str          # "spring-boot"
    tech_hints:       list[str]    # ["db:mysql", "mq:rabbitmq"]
    api_paths:        list[str]    # ["/api/v1/preserve"]
    # Топологическое
    pagerank:         float
    in_degree:        int
    out_degree:       int
    scc_id:           int | None
    # Качество
    has_tests:        bool
    churn_score:      float        # 0.0 = не менялся, 1.0 = меняется каждый коммит
    # Человеческий контекст
    doc_summary:      str          # Javadoc / @ApiOperation / fallback Ollama
```

---

## Итоговая схема поиска

```
Запрос пользователя
       │
       ▼
_is_identifier_query?
  да: alpha=0.3      нет: alpha=0.7
       │                    │
       └─────────┬──────────┘
                 ▼
    Hybrid(dense=emb_semantic, sparse=BM25, alpha)
                 │
                 ▼
         HNSW top-K nodes
                 │
         ┌───────┴────────┐
         ▼                ▼
   BFS по графу    SIBLING_OF (emb_struct)
   (явные связи)   (структурное сходство)
         │                │
         └───────┬─────────┘
                 ▼
          blast radius set
```

---

## Связи с ADR

- **ADR-014**: embedding-классификатор (Component A) использует `emb_semantic` с `alpha=0.7`
- **ADR-020**: роли ENV_PATTERN, CODE_STRUCTURE → маппинг на `emb_struct` / `emb_semantic`
- **ADR-022**: embedding roles table → уточняется этим ADR (три гранулярности вместо одной)
- **ADR-023**: bi-encoder реранкер (§H) → использует `emb_semantic`
- **ADR-027**: FileSimilarityIndex → использует `emb_struct` + HNSW

---

## Дополнение: стратегия чанкинга кода для embedding

**Проблема:** произвольная нарезка файла по строкам разрезает метод посередине,
разделяя сигнатуру от тела, или комментарий от функции, к которой он относится.
Это ухудшает качество эмбеддинга (`emb_semantic`).

### Правило: чанкинг по AST-границам

Единица чанкинга = **один метод/функция** (включая аннотации, docstring, тело).
Для больших классов — разбиение по методам с «оберткой» класса.

```
Единица emb_struct   (~150 токенов):
  @Annotation
  ReturnType methodName(ParamType param)
  ← сигнатура + аннотации; тело не включается

Единица emb_semantic (~400 токенов):
  Javadoc-блок / logger.info-строки / китайские комментарии
  + сигнатура метода
  + вызовы внешних сервисов из тела
  ← NB: комментарии и сигнатура в одном чанке

Единица emb_full     (~1500 токенов):
  весь метод целиком или весь небольшой класс
```

### Чанкинг комментариев в заданных пределах

Многострочные комментарии дробить по **предложениям/логическим блокам**
до достижения ~80 токенов, а не по фиксированному числу строк:

```
# Плохо (по строкам):
[строка 1-5 комментария] ← конец чанка
[строка 6-10 + начало метода] ← новый чанк  → смысловой разрыв

# Хорошо (по логическим блокам):
[весь логически связанный комментарий + сигнатура метода] ← один чанк
```

**Реализация (static, без LLM):**
- Разделитель чанка = строка с объявлением метода/функции (`def`, `void`, `func`)
- Перед каждым методом — предшествующие комментарии (до первой пустой строки)
- Overlap = сигнатура класса-контейнера (если метод в классе)

### Tiered importance (static, через аннотации)

Вместо Gemini Flash Lite — аннотации и паттерны имён:

| Уровень | Java-признаки | Python-признаки | В embedding |
|---|---|---|---|
| HIGH | `@RestController`, `@Service`, `@RequestMapping`, конструкторы, `main()` | `@app.route`, `app.run`, `socketio.run` | `emb_struct` + `emb_semantic` + `emb_full` |
| MEDIUM | `@Repository`, `@Component`, методы вызывающие `restTemplate`/`feign` | бизнес-логика, `requests.get` | `emb_struct` + `emb_semantic` |
| LOW | геттеры/сеттеры, `toString()`, `equals()`, методы только с `logger.*` | `__repr__`, тривиальные property | только `emb_struct` или пропустить |

LOW-узлы не индексируются в `emb_semantic`/`emb_full` — экономия места в LanceDB
и снижение шума при retrieval. Реализация: `_classify_method_tier()` в будущем парсере.
