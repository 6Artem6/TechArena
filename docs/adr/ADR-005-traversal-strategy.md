# ADR-005: Стратегия обхода графа для Blast Radius

**Статус:** Принято  
**Дата:** 2026-10-13

## Контекст

Центральная операция движка: от `change_spec` до плоского множества затронутых узлов. Нужно определить направление, глубину и правила обхода.

## Решение

### Входной сигнал (change_spec)

```python
{
    "file": "ts-order-service/src/main/java/order/service/OrderServiceImpl.java",
    "class": "order.OrderServiceImpl",      # опционально
    "method": "createOrder",                # опционально
    "config_section": None                  # для docker-compose / yml
}
```

Стартовые узлы определяются по специфичности (от конкретного к общему):
1. Если указан метод → стартуем с `METHOD`-узла
2. Если только класс → стартуем с `CLASS`-узла
3. Если только файл → стартуем с `CLASS`-узлов файла (их может быть несколько)
4. Если конфиг-секция → стартуем с `CONFIG_SECTION`-узла

### Направление обхода

Обход управляется таблицей разрешённых направлений по типу ребра. **Не все рёбра двунаправленные.**

| Тип ребра | Вперёд | Назад | Причина |
|---|---|---|---|
| `CONTAINS` | ✅ | ✅ | Метод ↔ класс ↔ сервис: изменение любого затрагивает оба конца |
| `CALLS` | ✅ | ✅ | Вызывающий и вызываемый: изменение сигнатуры затрагивает обоих |
| `CALLS_SERVICE` | ✅ | ✅ | Межсервисная связь: изменение API затрагивает вызывающего |
| `USES_DTO` | ✅ | ⛔ | **Критично**: DTO не разворачивает к всем потребителям. Если изменился потребитель — не идём через DTO к другим потребителям. Если изменился сам DTO — `start_node.node_type == "DTO"` → тогда назад разрешено |
| `IMPLEMENTS` | ✅ | ✅ | Интерфейс ↔ реализация |
| `SHARES_RESOURCE` | ✅ | ✅ | Общий ресурс: изменение схемы БД затрагивает всех потребителей |
| `ASYNC_MESSAGE` | ✅ | ✅ | Очередь: изменение формата сообщения затрагивает и sender, и receiver |
| `USES_MAGIC_CALL` | ✅ | ⛔ | `UnknownDynamicTarget` — терминальный лист, назад не идём |
| `SHADOW_*` | ✅ | ⛔ | Shadow рёбра — только вперёд, не разворачиваем |

```python
# Таблица управления обходом: (forward, backward)
TRAVERSAL_POLICY: dict[str, tuple[bool, bool]] = {
    "CONTAINS":              (True,  True),
    "CALLS":                 (True,  True),
    "CALLS_SERVICE":         (True,  True),
    "USES_DTO":              (True,  False),  # ← без этого ts-common взрывает весь граф
    "IMPLEMENTS":            (True,  True),
    "SHARES_RESOURCE":       (True,  True),
    "ASYNC_MESSAGE":         (True,  True),
    "USES_MAGIC_CALL":       (True,  False),
    "SHADOW_CALLS":          (True,  False),
    "SHADOW_CALLS_SERVICE":  (True,  False),
    "SHADOW_USES_DTO":       (True,  False),
    "SHADOW_SHARES_RESOURCE":(True,  False),
    "SEMANTIC_BRIDGE":       (True,  False),
}

def get_neighbors(node: NodeId, graph: nx.MultiDiGraph,
                  start_is_dto: bool = False) -> set[NodeId]:
    """Возвращает соседей с учётом политики направления рёбер."""
    neighbors = set()
    for _, tgt, data in graph.out_edges(node, data=True):
        policy = TRAVERSAL_POLICY.get(data["edge_type"], (True, False))
        if policy[0]:  # forward разрешён
            neighbors.add(tgt)
    for src, _, data in graph.in_edges(node, data=True):
        policy = TRAVERSAL_POLICY.get(data["edge_type"], (True, False))
        backward_ok = policy[1]
        # Исключение: USES_DTO назад разрешён только от самого DTO
        if data["edge_type"] == "USES_DTO" and not start_is_dto:
            backward_ok = False
        if backward_ok:
            neighbors.add(src)
    return neighbors
```

### Алгоритм

```
BFS(start_nodes, graph, max_depth):
    visited = set(start_nodes)
    queue = deque([(node, 0) for node in start_nodes])
    
    while queue:
        node, depth = queue.popleft()
        if depth >= max_depth:
            continue
        
        # Все соседи: и входящие, и исходящие рёбра
        neighbors = set(graph.successors(node)) | set(graph.predecessors(node))
        
        for neighbor in neighbors:
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append((neighbor, depth + 1))
    
    return visited
```

### Параметры

| Параметр | Default | Описание |
|---|---|---|
| `max_depth` | 6 | Максимальная глубина обхода (защита от зависания) |
| `include_magic_calls` | `True` | Включать ли `UnknownDynamicTarget` узлы |
| `edge_types` | все | Фильтр по типам рёбер (для отладки) |

**Почему 6, а не 3?** Глубина 3 была предложена для кэширования метрик критичности. Для самого обхода blast radius 3 уровня слишком мало: метод → класс → сервис → другой сервис → класс → метод = уже 5 шагов для минимальной межсервисной цепочки.

### Про "затухание" (clarification)

Явного затухания нет. Вместо него:
- Обход останавливается у `max_depth`.
- `UnknownDynamicTarget` узлы включаются, но не распространяются дальше по своим рёбрам (они — терминальные листья).
- В фазе 4: `criticality` и веса рёбер добавляются как атрибуты в вывод, но не влияют на сам обход — это дополнительная информация для жюри.

### Единая формула весов (risk_score)

Все источники весов объединяются в одну формулу для каждого узла в результате:

```python
def compute_node_risk_score(node_id: NodeId, incoming_edges: list[Edge],
                             node_metadata: dict) -> float:
    """Итоговый риск узла = max по всем входящим рёбрам (не сумма, не среднее).
    
    Логика: если хотя бы одно высокорисковое ребро указывает на узел, он рискован.
    """
    scores = []
    for edge in incoming_edges:
        # Базовый вес по типу ребра (ADR-002)
        base = EDGE_WEIGHTS[edge.edge_type]          # CALLS=1.0, USES_DTO=0.3, ...
        
        # Shadow коэффициент (ADR-016)
        shadow = 0.4 if edge.metadata.get("shadow") else 1.0
        
        # Уверенность классификации (ADR-014)
        confidence = edge.metadata.get("confidence_score", 1.0)
        
        # Критичность источника (фаза 4: пока = 1.0)
        src_criticality = node_metadata.get("criticality", 1.0)
        
        scores.append(base * shadow * confidence * src_criticality)
    
    return max(scores) if scores else 0.5  # 0.5 = стартовый узел

EDGE_WEIGHTS = {
    "CALLS":              1.0,
    "CALLS_SERVICE":      1.0,
    "USES_DTO":           0.3,
    "IMPLEMENTS":         0.8,
    "IMPORTS":            0.5,
    "SHARES_RESOURCE":    0.5,
    "USES_MAGIC_CALL":    1.5,
    "ASYNC_MESSAGE":      0.8,
    "SHADOW_CALLS":       0.4,
    "SHADOW_CALLS_SERVICE": 0.4,
    "SHADOW_USES_DTO":    0.15,
    "SHADOW_SHARES_RESOURCE": 0.2,
}
```

`risk_score` — атрибут в JSON-выводе каждого узла. BFS-обход остаётся структурным (посетить/нет), risk_score не влияет на включение в множество.

### Семантический мост (BFS + Semantic Search)

Два сценария, когда семантический поиск дополняет BFS:

**Сценарий 1: Мёртвый тупик (dead end)**

BFS достигает узла без исходящих/входящих рёбер (слабо связанный сервис, `PARTIAL_PARSED`).  
Вместо тихого останова — искать семантически похожие узлы:

```python
def bfs_with_semantic_bridge(start: NodeId, graph: nx.MultiDiGraph,
                              embeddings: dict[NodeId, np.ndarray],
                              max_depth: int = 6,
                              semantic_threshold: float = 0.82) -> set[NodeId]:
    visited = set()
    queue = deque([(node, 0) for node in resolve_start_nodes(start)])
    
    while queue:
        node, depth = queue.popleft()
        if node in visited or depth >= max_depth:
            continue
        visited.add(node)
        
        structural_neighbors = (set(graph.successors(node)) |
                                 set(graph.predecessors(node)))
        
        # Мост: если структурных соседей нет → ищем семантически близкие
        if not structural_neighbors and node in embeddings:
            semantic_candidates = find_nearest_nodes(
                embeddings[node], embeddings,
                top_k=3, threshold=semantic_threshold,
                exclude=visited
            )
            # Помечаем: эти узлы добавлены через семантику, не структуру
            for candidate in semantic_candidates:
                graph.add_edge(node, candidate,
                               edge_type="SEMANTIC_BRIDGE",
                               weight=0.3,
                               metadata={"synthetic": True})
            structural_neighbors = set(semantic_candidates)
        
        for neighbor in structural_neighbors:
            if neighbor not in visited:
                queue.append((neighbor, depth + 1))
    
    return visited
```

`SEMANTIC_BRIDGE` рёбра — синтетические (не сохраняются в PostgreSQL), используются только при обходе.

**Сценарий 2: ts-common взрыв (слишком широкий результат)**

Изменение DTO в `ts-common` → BFS возвращает 200+ узлов (всё через `USES_DTO`).  
Ранжирование по semantic_similarity к изменённому узлу обрезает "дальние" результаты:

```python
def rank_blast_radius(visited: set[NodeId], start: NodeId,
                      embeddings: dict[NodeId, np.ndarray],
                      top_k: int | None = None) -> list[tuple[NodeId, float]]:
    """Ранжирует узлы blast radius по семантической близости к стартовому."""
    if start not in embeddings:
        return [(n, 1.0) for n in visited]  # нет эмбеддинга → все равнозначны
    
    start_vec = embeddings[start]
    ranked = [(node, cosine_similarity(start_vec, embeddings[node]))
              for node in visited if node in embeddings]
    ranked.sort(key=lambda x: -x[1])
    
    return ranked[:top_k] if top_k else ranked
```

`SEMANTIC_BRIDGE` и ранжирование — опциональные фазы, включаются флагом `TraversalConfig.semantic_bridge=True`.

### Параллелизм BFS

#### GIL и выбор примитива параллелизма

NetworkX — чистый Python, GIL не отпускается при BFS. `ThreadPoolExecutor` для BFS даёт псевдопараллелизм без реального ускорения CPU-bound работы.  
**Правило:** для параллельного BFS — только `ProcessPoolExecutor` или матричные операции (scipy/BLAS, которые многопоточны на уровне C).

#### Уровень 1: fork-shared graph (бесплатно на Linux/Mac)

`os.fork()` создаёт copy-on-write копию памяти. Если дочерние процессы только читают граф — копирования не происходит. Это основа `--workers N` в uvicorn (ADR-010):

```python
# В FastAPI lifespan: загружаем граф ДО форка (при старте мастер-процесса)
# Все воркеры наследуют граф через CoW — N параллельных BFS без N×RAM
_graph: nx.MultiDiGraph = load_graph_snapshot()  # один раз в мастере
```

#### Уровень 2: Параллельные BFS от разных стартов

Если `change_spec` содержит несколько файлов из **разных сервисов** — их BFS почти не пересекаются → embarrassingly parallel:

```python
async def compute_blast_radius_parallel(starts: list[NodeId],
                                         executor: ProcessPoolExecutor) -> set[NodeId]:
    loop = asyncio.get_event_loop()
    futures = [loop.run_in_executor(executor, bfs_from_single, s) for s in starts]
    results = await asyncio.gather(*futures)
    return set.union(*results)
```

При стартах из одного сервиса — пересечение велико, объединять лучше в одном BFS.

#### Уровень 3: Матричный BFS (scipy sparse + BLAS)

Scipy строит sparse adjacency matrix → `A @ frontier` = одновременное расширение всего фронта через BLAS (Intel MKL / OpenBLAS — многопоточны автоматически):

```python
# При загрузке pickle: строится ОДИН РАЗ, сохраняется рядом с графом
A_sparse = nx.to_scipy_sparse_array(graph, format="csr")
node_index = {n: i for i, n in enumerate(graph.nodes())}

def bfs_matrix(start_indices: list[int], max_depth: int = 6) -> np.ndarray:
    """Параллельный BFS через матрично-векторное умножение."""
    frontier = np.zeros(len(node_index), dtype=bool)
    frontier[start_indices] = True
    visited = frontier.copy()
    for _ in range(max_depth):
        neighbors = (A_sparse @ frontier.astype(float) +
                     A_sparse.T @ frontier.astype(float)) > 0
        new = neighbors & ~visited
        if not new.any():
            break
        visited |= new
        frontier = new
    return visited  # bool-маска → конвертировать в NodeId через index
```

Применять когда граф > 5000 узлов или нужны сотни BFS-запросов подряд.

#### Landmark pre-computation (ключевое для хакатона)

Вместо ускорения BFS — устранить BFS для "горячих" узлов:

```python
# make index → вычисляет и кеширует blast radius от ключевых узлов
LANDMARK_NODES = [
    "ts-common::edu.fudan.common.entity.Order",    # 40+ потребителей
    "ts-common::edu.fudan.common.util.Response",   # Response wrapper везде
    "ts-gateway-service",                          # точка сочленения
    "ts-security-service",                         # JWT filter везде
]

# При запросе: если стартовый узел — landmark → O(1) из Redis
# Если достижим из landmark за 1-2 шага → combine с landmark cache
# Иначе → обычный BFS
```

TTL ключей: до следующей переиндексации. Инвалидация: при новом pickle.

#### Сервисное шардирование (для репозиториев > 50k узлов)

Разбить граф на субграфы по сервисам → независимые BFS-процессы → синхронизация через `multiprocessing.Queue` при пересечении CALLS_SERVICE рёбер. Для train-ticket (3000 узлов) не нужно — упоминается как стратегия масштабирования.

### Алгоритмические улучшения обхода

#### Двунаправленный BFS (Bidirectional BFS)

Текущий BFS: O(b^d), при b≈10 и d=6 → ~10^6 операций.  
Bidirectional BFS запускает встречный обход одновременно из стартовых узлов и в обратном направлении. Сложность: O(b^(d/2)) → ~10^3 для тех же параметров.

```python
# Применяется когда graph.number_of_nodes() > 1000 и max_depth >= 4
# Стандартный BFS сохраняется для малых графов и отладки
traversal_mode = "bidirectional" if len(graph) > 1000 else "standard"
```

#### SCC — предобработка циклических зависимостей

Алгоритм Косараджу/Тарьяна за O(V+E) находит все сильносвязные компоненты. Компоненты с >1 узлом = "blast clusters": изменение любого узла → весь кластер в blast radius.

```python
# Выполняется ОДИН РАЗ при загрузке pickle, результат кешируется
sccs = [scc for scc in nx.strongly_connected_components(graph) if len(scc) > 1]
scc_lookup: dict[NodeId, frozenset] = {
    node: frozenset(scc) for scc in sccs for node in scc
}
# При BFS: если стартовый узел в SCC → добавить весь кластер немедленно
```

#### DSU для SHARES_RESOURCE кластеров

Disjoint Set Union строится при загрузке графа по всем `SHARES_RESOURCE` рёбрам. O(α(n)) ≈ O(1) на запрос "какие сервисы в одном DB/queue кластере".

```python
resource_dsu = build_dsu(edge for edge in graph.edges if edge.type == "SHARES_RESOURCE")
# При BFS: узел SERVICE + SHARES_RESOURCE → все сервисы в кластере добавляются сразу
```

#### Точки сочленения — автоматическая критичность

```python
# Вычисляется при построении pickle, хранится в node metadata
articulation_points = set(nx.articulation_points(graph.to_undirected()))
for node in articulation_points:
    graph.nodes[node]["criticality"] = "HIGH"  # без Ollama, структурно
    graph.nodes[node]["is_bridge"] = True
```

#### LCA для множественных изменений

Когда change_spec содержит >1 файла, LCA в иерархии CONTAINS находит минимальный общий предок — стартовый узел для объединённого BFS.

```python
# Binary lifting: O(n log n) препроцессинг, O(log n) на запрос
# Упрощённо: если два метода из одного класса → стартуем с класса, не с методов
```

#### Структурное хэширование для клонов

```python
# При индексации: каждому AST-узлу присваивается structural_hash (Merkle-подход)
def structural_hash(node_type: str, children_hashes: list[int]) -> int:
    return hash((node_type, tuple(sorted(children_hashes))))

# Результат: graph.nodes[id]["structural_hash"] = int
# GROUP BY structural_hash → мгновенный поиск структурно идентичных методов
```

ts-order-service и ts-order-other-service — структурные клоны. Находятся за O(1) по хэшу.  
Если изменённый метод имеет клоны → все клоны входят в blast radius автоматически.

### Граф в памяти: NetworkX Pickle Snapshot

BFS работает **полностью в памяти** — без обращений к PostgreSQL во время обхода.

**Схема:**

```
[worker-service]                      [engine-api]
  Индексация завершена                  Старт / перезагрузка
       │                                       │
       ▼                                       ▼
  Сохранить pickle                      Загрузить pickle
  /data/graph.pkl                       → self._graph (NetworkX)
  (NetworkX MultiDiGraph)                    │
       │                                       ▼
       ▼                              BFS по self._graph
  Сообщить в Redis:                   (0 запросов к БД)
  graph:snapshot:updated
```

```python
# worker-service: после завершения индексации
import pickle, pathlib

def save_graph_snapshot(graph: nx.MultiDiGraph, path: str = "/data/graph.pkl") -> None:
    """Сохраняет граф как pickle для последующей загрузки API."""
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump(graph, f, protocol=pickle.HIGHEST_PROTOCOL)

# engine-api: при старте (lifespan)
def load_graph_snapshot(path: str = "/data/graph.pkl") -> nx.MultiDiGraph | None:
    """Загружает граф из pickle. Возвращает None если файл отсутствует."""
    if not pathlib.Path(path).exists():
        return None
    with open(path, "rb") as f:
        return pickle.load(f)
```

**Разделение по уровням абстракции для снижения нагрузки на старт:**

Если pickle отсутствует (первый запуск без индексации), граф строится из PostgreSQL.  
Чтобы не грузить 3000+ узлов сразу, используется поуровневая загрузка:

```sql
-- Сначала только SERVICE и CLASS (быстро, мало записей)
SELECT * FROM nodes WHERE node_type IN ('SERVICE', 'CLASS', 'INTERFACE');
-- Потом METHOD (их большинство) — по требованию при первом запросе к конкретному сервису
SELECT * FROM nodes WHERE node_type = 'METHOD' AND service = $1;
```

Lazy-loading по уровням: SERVICE/CLASS всегда в памяти, METHOD-узлы подгружаются при первом запросе к сервису и кешируются. После полной прогрузки граф эквивалентен pickle.

**Инвалидация при переиндексации:**

- Один запрос blast radius = анализ одного `change_spec`, граф не меняется во время работы.
- После новой индексации: воркер создаёт новый pickle → API перезагружает при следующем старте.
- Redis-ключ `graph:snapshot:version` — версия pickle для проверки актуальности.

## Обоснование

- Полный двунаправленный обход максимизирует recall — приоритет до получения baseline.
- `max_depth=6` покрывает типичные межсервисные цепочки без риска зависания на циклических графах.
- NetworkX в памяти: BFS по 3000 узлов ≈ 1–5 мс, без DB round-trip.
- Pickle snapshot: создаётся воркером один раз, читается API при старте — минимальная связанность.

## Последствия

- Shared volume между `worker-service` и `engine-api` для передачи pickle (`/data/graph.pkl`).
- При `max_depth=6` на `ts-common` изменениях граф может вернуть большую часть репозитория — ожидаемое поведение, корректируется baseline.
- После получения baseline — первым делом откалибровать `max_depth` и флаг `include_magic_calls`.
