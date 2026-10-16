# ADR-027: Сервисы-родственники (SIBLING_OF), семантика файлов, кросс-сервисные эмбеддинги

**Статус:** Принято  
**Дата:** 2026-10-13  
**Связанные:** ADR-002, ADR-005, ADR-017, ADR-020, ADR-021

---

## Контекст

В train-ticket выявлены пары/группы сервисов, реализующих одну функциональность для разных подмножеств данных. Изменение в одном почти всегда требует аналогичного изменения в его «родственнике». Это влияет на blast radius.

---

## A. Реальные sibling-пары в train-ticket (верифицировано)

| Группа | Сервисы | Условие разделения | Порты |
|---|---|---|---|
| order | `ts-order-service` / `ts-order-other-service` | `trainNumber.startsWith("G\|D")` → order; иначе → order-other | 12031/12032 |
| preserve | `ts-preserve-service` / `ts-preserve-other-service` | то же: G/D → preserve; иначе → preserve-other | 14568/14569 |
| travel | `ts-travel-service` / `ts-travel2-service` | G/D поезда → travel; другие → travel2 | 12346/16346 |
| food | `ts-food-service` / `ts-station-food-service` / `ts-train-food-service` / `ts-food-map-service` | по типу: menu/station/train/map | разные |
| consign | `ts-consign-service` / `ts-consign-price-service` | по функции: доставка / цена доставки | 16111/16110 |
| admin | `ts-admin-order-service` / `ts-admin-user-service` / ... | по домену: admin-обёртка | разные |
| route | `ts-route-service` / `ts-route-plan-service` | данные маршрута / планирование | 11178/14578 |
| delivery | `ts-delivery-service` / `ts-food-delivery-service` | логистика / еда | — |

**Структурное подтверждение** (order ↔ order-other):
```
ts-order-service:       OrderController, OrderService, OrderServiceImpl
ts-order-other-service: OrderOtherController, OrderOtherService, OrderOtherServiceImpl
→ классы идентичны по структуре с суффиксом "Other"

ts-preserve-service:        PreserveController, PreserveServiceImpl, Queues, RabbitSend
ts-preserve-other-service:  PreserveOtherController, PreserveOtherServiceImpl, Queues, RabbitSend
→ зеркальная структура

ts-travel-service:  TravelController, TravelServiceImpl, Trip, TripRepository
ts-travel2-service: Travel2Controller, TravelServiceImpl(!)  ← одинаковое имя класса
```

**CFG-условие в SeatServiceImpl.java:**
```java
// Комментарий самого кода:
// "Distinguish G\D from other trains"
if (trainNumber.startsWith("G") || trainNumber.startsWith("D")) {
    // → ts-order-service
} else {
    // → ts-order-other-service
}
```

---

## B. Алгоритм обнаружения siblings (три фазы)

### Фаза 1: Rule-based (бесплатно, O(n))

```python
SIBLING_RULES = [
    # Паттерн: (regex для name_a, regex для name_b, split_type)
    (r'^ts-(.+)-service$',           r'^ts-\1-other-service$',         'MIRROR_OTHER'),
    (r'^ts-(.+)-service$',           r'^ts-\12-service$',              'MIRROR_NUMERIC'),
    (r'^ts-admin-(.+)-service$',     r'^ts-\1-service$',               'ADMIN_WRAPPER'),
    (r'^ts-(.+)-service$',           r'^ts-\1-plan-service$',          'PLAN_VARIANT'),
    (r'^ts-(.+)-service$',           r'^ts-\1-price-service$',         'PRICE_VARIANT'),
    (r'^ts-(.+)-service$',           r'^ts-\1-delivery-service$',      'DELIVERY_VARIANT'),
    (r'^ts-(.+)-service$',           r'^ts-station-\1-service$',       'STATION_VARIANT'),
    (r'^ts-(.+)-service$',           r'^ts-train-\1-service$',         'TRAIN_VARIANT'),
]

def detect_siblings_by_rules(services: list[str]) -> list[SiblingPair]:
    pairs = []
    svc_set = set(services)
    for a in services:
        for pat_a, pat_b_tmpl, split_type in SIBLING_RULES:
            m = re.match(pat_a, a)
            if m:
                b = re.sub(r'\\1', m.group(1), pat_b_tmpl)
                if b in svc_set and b != a:
                    pairs.append(SiblingPair(a, b, split_type, method='rule'))
    return pairs
```

**Результат для train-ticket (Rule-based):**
```
MIRROR_OTHER:    order ↔ order-other, preserve ↔ preserve-other
MIRROR_NUMERIC:  travel ↔ travel2
ADMIN_WRAPPER:   admin-order → order, admin-user → user, ...
PLAN_VARIANT:    route ↔ route-plan, travel ↔ travel-plan
PRICE_VARIANT:   consign ↔ consign-price
DELIVERY_VARIANT: food ↔ food-delivery
TRAIN_VARIANT:   food ↔ train-food
STATION_VARIANT: food ↔ station-food
```

### Фаза 2: Структурная схожесть (только для не найденных правилами)

Сравниваем наборы имён классов между сервисами:

```python
def structural_similarity(svc_a: str, svc_b: str, symbol_index: SymbolIndex) -> float:
    """Jaccard по нормализованным именам классов."""
    def normalize_class_names(svc: str) -> set[str]:
        classes = symbol_index.get_classes(svc)
        # Убрать service-specific prefix: OrderOtherServiceImpl → ServiceImpl
        return {re.sub(r'^[A-Z][a-z]+(?:Other|2)?', '', c) for c in classes}
    
    a_set = normalize_class_names(svc_a)
    b_set = normalize_class_names(svc_b)
    if not a_set or not b_set:
        return 0.0
    return len(a_set & b_set) / len(a_set | b_set)

# Порог: Jaccard >= 0.6 → sibling
```

**Результат для order ↔ order-other:**
```
a_set = {Controller, Service, ServiceImpl}  (убрали "Order")
b_set = {Controller, Service, ServiceImpl}  (убрали "OrderOther")
Jaccard = 3/3 = 1.0  → SIBLING
```

### Фаза 3: BGE-M3 bi-encoder (только для неизвестных, < 47 сервисов)

Применять только если фазы 1 и 2 не дали результата:

```python
def detect_siblings_by_embedding(
    services: list[str],
    symbol_index: SymbolIndex,
    encoder,
    threshold: float = 0.82,
) -> list[SiblingPair]:
    """
    Контекст для каждого сервиса: имя + топ-3 endpoint-пути + список классов.
    47 сервисов × 47 = 2209 пар → быстро даже с энкодером.
    """
    contexts = {}
    for svc in services:
        endpoints = symbol_index.get_endpoints(svc)[:3]
        classes = symbol_index.get_classes(svc)[:5]
        contexts[svc] = f"[SVC:{svc}][EP:{','.join(endpoints)}][CLS:{','.join(classes)}]"
    
    embeddings = {svc: encoder.encode(ctx) for svc, ctx in contexts.items()}
    
    pairs = []
    svcs = list(services)
    for i, a in enumerate(svcs):
        for b in svcs[i+1:]:
            sim = cosine_similarity(embeddings[a], embeddings[b])
            if sim >= threshold:
                pairs.append(SiblingPair(a, b, 'SEMANTIC', method='embedding', sim=sim))
    return pairs
```

---

## C. Граф: ребро SIBLING_OF

```python
@dataclass
class SiblingEdge:
    service_a: str
    service_b: str
    split_type: str           # MIRROR_OTHER / MIRROR_NUMERIC / ADMIN_WRAPPER / ...
    split_condition: str      # "trainNumber.startsWith('G') || trainNumber.startsWith('D')"
    confidence: float         # 1.0 для rule, Jaccard для structural, cosine для embedding
    method: str               # 'rule' / 'structural' / 'embedding'

# В графе:
G.add_edge(svc_a, svc_b,
    type='SIBLING_OF',
    split_type=edge.split_type,
    split_condition=edge.split_condition,
    conf=edge.confidence,
)
```

**split_condition** берётся из CFG-анализа (ADR-021): когда в коде найдено
`if (trainNumber.startsWith("G")) → getServiceUrl("ts-travel-service") else → getServiceUrl("ts-travel2-service")` — это условие сохраняется в атрибуте ребра.

---

## D. Влияние на blast radius

```
TRAVERSAL_POLICY: SIBLING_OF → (forward=True, backward=True, depth_cost=0)
Вес ребра: MIRROR_OTHER=0.85, MIRROR_NUMERIC=0.85, ADMIN_WRAPPER=0.6, PLAN_VARIANT=0.5
```

**Логика:** изменение в `ts-order-service` с высокой вероятностью (0.85) затрагивает `ts-order-other-service` — они реализуют одну схему данных. Но не с полным весом (1.0): они деплоятся раздельно, у них отдельные MongoDB.

```python
# При BFS: если текущий узел = SERVICE, проходить SIBLING_OF рёбра как
# бесплатные по depth (depth_cost=0), но с весом ниже 1.0
if edge_type == 'SIBLING_OF' and node_type == 'SERVICE':
    sibling_weight = SIBLING_WEIGHTS.get(split_type, 0.7)
    enqueue(neighbor, depth=current_depth, weight=current_weight * sibling_weight)
```

---

## E. Семантика файлов: кросс-сервисные эмбеддинги

### Идея

Находить **функционально близкие файлы** между разными сервисами. Применения:

1. **Верификация sibling-ов**: если два файла из разных сервисов имеют cosine ≥ 0.88 → оба сервиса вероятно siblings
2. **Распространение техдолга**: если в `OrderServiceImpl` найден паттерн техдолга — проверить `OrderOtherServiceImpl` тем же детектором
3. **Cross-service IoU improvement**: изменение в файле с высоким cross-service similarity → blast radius включает похожий файл из sibling

### Алгоритм

```python
@dataclass
class FileSimilarityIndex:
    """
    Индекс кросс-сервисных близких файлов.
    Строится ОДИН РАЗ при индексации, хранится в LanceDB.
    """
    def build(self, all_files: list[SourceFile], encoder) -> None:
        embeddings = {}
        for f in all_files:
            if f.service == 'ts-common':  # общий код — пропустить
                continue
            ctx = self._file_context(f)
            embeddings[f.id] = encoder.encode(ctx)
        
        # Находим похожие пары (только разные сервисы!)
        for fa in all_files:
            candidates = vector_search(embeddings[fa.id], top_k=5)
            for fb, sim in candidates:
                if fb.service != fa.service and sim >= 0.85:
                    self._store(fa, fb, sim)
    
    def _file_context(self, f: SourceFile) -> str:
        """Контекст для эмбеддинга: нормализованное имя + аннотации + методы."""
        # Убираем service-specific prefix: "Order" из "OrderServiceImpl"
        normalized_name = normalize_class_name(f.class_name, f.service)
        annotations = ' '.join(f.annotations[:5])
        methods = ' '.join(m.name for m in f.methods[:8])
        return f"[CLS:{normalized_name}][ANN:{annotations}][METHODS:{methods}]"
```

**Нормализация имён классов для сравнения:**
```python
def normalize_class_name(class_name: str, service: str) -> str:
    """
    OrderServiceImpl (ts-order-service) → ServiceImpl
    OrderOtherServiceImpl (ts-order-other-service) → ServiceImpl
    PreserveOtherController → Controller
    Travel2Application → Application
    """
    # Извлечь доменный префикс из имени сервиса: ts-order-service → Order
    domain = ''.join(w.capitalize() for w in service
                     .replace('ts-', '').replace('-service', '').split('-'))
    # Убрать домен и его варианты из имени класса
    pattern = re.compile(rf'^{domain}(?:Other|2)?')
    return pattern.sub('', class_name) or class_name
```

**Результат для travel/travel2:**
```
travel/TravelServiceImpl    → ServiceImpl
travel2/TravelServiceImpl   → ServiceImpl  (уже совпадает!)
→ cosine будет очень высоким → подтверждает sibling
```

### Использование в blast radius

```python
def expand_blast_radius_via_siblings(
    affected: set[str],
    sibling_index: SiblingEdge,
    file_sim_index: FileSimilarityIndex,
) -> set[str]:
    """
    Для каждого затронутого файла — добавить похожие файлы из sibling-сервисов
    с весом пропорционально cosine similarity.
    """
    extra = set()
    for node_id in affected:
        similar = file_sim_index.get_similar(node_id, min_sim=0.85)
        for sim_node, sim in similar:
            if is_sibling_service(node_id, sim_node, sibling_index):
                extra.add(sim_node)
    return extra
```

---

## F. Итог: что добавляем

| Компонент | Где | Вес для IoU |
|---|---|---|
| `SIBLING_OF` ребро | Граф, Tier 1 (rule-based) | Высокий — order/order-other всегда вместе |
| `split_condition` на ребре | Из CFG-анализа (ADR-021) | Документация логики разделения |
| `FileSimilarityIndex` | LanceDB, Tier 3 | Средний — верификация и расширение |
| Нормализация имён классов | SymbolIndex, Tier 2 | — |
| BGE-M3 sibling detection | Tier 4 (если rule не нашло) | Низкий для train-ticket (rule покрывает всё) |

**Для train-ticket rule-based фаза покрывает 100% siblings** — BGE-M3 не нужен.  
`FileSimilarityIndex` — Tier 3: строить после базового графа.
