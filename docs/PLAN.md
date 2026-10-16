# План реализации — Execution Trace Predictor

> **Задача хакатона:** по текстовому сценарию (S0–S7) предсказать execution trace —
> какие методы выполнились, какие вызовы между методами/сервисами произошли, какие таблицы затронул SQL.
> Скелет (`skeleton/trainticket.json.gz`) **дан организаторами как подсказка**, а не готовый ответ.
> Некоторые реальные вызовы скелет может не содержать.

---

## Проверенные условия задания

| Условие | Статус |
|---|---|
| Скелет предоставлен организаторами | ✅ `skeleton/trainticket.json.gz` — 15 229 узлов, 19 573 рёбра |
| Скелет — may-граф, не exact trace | ✅ "не содержит всех нужных ответов"; некоторые вызовы могут отсутствовать |
| NodeId из скелета строго | ✅ "сокращения и придуманные идентификаторы не принимаются" — копировать из `nodes` |
| Источник `calls` — метод, не call_site | ✅ поле `method` у вершины `call_site`; baseline_closure.py: `cs_method[]` |
| `stmt:` узлы входят в executes | ✅ baseline_closure.py добавляет их в submission через `stmts[]` |
| S0 **не** входит в итоговый балл | ✅ "открытый S0 нужен для разработки"; итог по S1–S7 |
| baseline_nlp.py уже читает UI (ts-ui-dashboard) | ✅ двойной BM25: code_doc + ui_doc; top-1 per step |
| baseline_nlp.py на скрытых сценариях ~0.50 | ✅ "средний результат около 0,50"; на S0 = 0.431 |
| Скелет: calls идёт от call_site, не от метода | ✅ трансформация нужна для `calls` в ответе |
| deployment_units: ts-common входит в каждый сервис | ✅ `closure()` обходит только по unit; ts-common доступен из любого |
| Надбавка +10% за Rust | ✅ после проверки кода организаторами |
| Сценарии не наследуют изменения друг друга | ✅ каждый стартует с `db-after-warmup.sql.gz` |
| "Первый" → первый в порядке ответа системы | ✅ не меняет код — только данные |

---

## Принципы

- **Скелет = Source of Truth для NodeId, но не для полноты.** ID только из скелета; вызовы — скелет + AST.
- **baseline_nlp.py — стартовая точка, не конкурент.** Улучшаем, не переписываем.
- **Итог по S1–S7, не S0.** S0 — калибровка; не переобучаться.
- **Precision-first для `remote`.** Gold remote=0 в части сценариев → одно лишнее ребро = IoU=0 (P-40, P-100).
- **Local-first / Static.** Никаких внешних вызовов во время анализа.
- **Каждая фаза — измеримый прирост IoU.**

---

## Соотношение старый план ↔ новый

| Старый план | Новый план | Изменение |
|---|---|---|
| Фаза 1: Построение графа с нуля | Фаза 1: Enrichment | Граф не строим; только code_signatures поверх скелета |
| Фаза 1.6: BFS blast radius | Фаза 3: BFS closure по скелету | По скелету; baseline_closure.py переиспользуется; + branch pruner |
| Фаза 1.7: JSON-контракт | `output/submission.py` | Формат: `{executes, calls, remote, data}` + stmt: узлы |
| Фаза 2: Обогащение графа | (часть Фазы 1) | Только code_signatures; граф не расширяется |
| Фаза 3: Семантический слой | Фаза 4 | Сдвинут в Tier 2+; фокус на улучшении entry point recall |
| Фаза 4: Веса и критичность | ~~убрана~~ | Не нужна для execution trace |

---

## Структура проекта (целевая)

```
src/
├── skeleton/              # Загрузка скелета
│   ├── loader.py          # trainticket.json.gz → nx.DiGraph + Index
│   └── index.py           # Index (расширяет baseline_closure.Index):
│                          #   handlers, wiring, call_map, module_map, stmts
├── enrichment/            # Обогащение code_signatures
│   ├── walker.py          # FileWalker (из старой Фазы 1.1)
│   ├── parser.py          # tree-sitter: @RequestMapping, logger-tags, flow-steps
│   └── signatures.py      # CodeSignature → привязка к NodeId скелета
├── localization/          # NLP: текст шага → NodeId контроллера
│   ├── step_typer.py      # Текст → step_type + параметры (тип поезда, класс места…)
│   ├── ui_context.py      # (из baseline_nlp.py) UI токены → обработчики
│   └── searcher.py        # BM25(code + UI) → NodeId; улучшенный baseline_nlp
├── trace/                 # BFS + Branch Selection
│   ├── oracle.py          # Stateful: balance, date, order_status, train_type, …
│   ├── pruner.py          # CFG condition evaluator (3a + 3b)
│   └── closure.py         # BFS (расширяет baseline_closure.Index.closure())
├── output/
│   └── submission.py      # Финальный submission.json с stmt: узлами
└── adapters/
    ├── parsers/            # TreeSitterJava (для enrichment)
    └── vector/             # LanceDB (Фаза 4)
```

---

## Фаза 0 — Baseline

**Цель:** воспроизвести baseline локально; зафиксировать разрывы по типам рёбер.

### 0.1 Запуск baseline_nlp.py

```bash
# Из tt-challenge/
python3 platform/baseline_nlp.py \
    skeleton/trainticket.json.gz \
    targets/trainticket \
    scenarios/*.json \
    -o my-answer.json --explain

python3 platform/evaluate_golden.py gold/S0.json my-answer.json
```

Ожидаемые числа на S0: executes=0.475, calls=0.533, remote=0.0, data=0.714, overall=0.431.  
На скрытых S1–S7: ≈0.50 (цель — значительно выше).

### 0.2 Анализ разрывов по типам

| Тип | S0 baseline | Целевое | Главная причина разрыва |
|---|---|---|---|
| `executes` | 0.475 | 0.65+ | неточный entry point + stmt: узлы |
| `calls` | 0.533 | 0.70+ | branch selection |
| `remote` | **0.000** | 0.40+ | нет алгоритма выбора remote рёбер |
| `data` | 0.714 | 0.80+ | ORM lazy, дочерние таблицы (user_roles…) |

### 0.3 Изучить baseline_nlp.py

Ключевые компоненты, которые **переиспользуем**:
- `ui_context()` — парсинг `ts-ui-dashboard/` JS/HTML → UI токены обработчиков
- `handler_docs()` — два документа: code + UI
- `BM25.rank()` — двойной поиск per step
- `entries_of()` — выбор top-1 per step
- `Index.closure()` — BFS с unit-boundary; трансформация call_site→метод
- `Index.submission()` — форматирование с stmt: узлами

**Точка измерения:** baseline воспроизведён; разрывы зафиксированы.

---

## Фаза 1 — Enrichment (Обогащение скелета)

**Цель:** добавить `code_signature` к NodeId скелета для улучшенного BM25-поиска.  
Эта фаза расширяет `handler_docs()` из baseline_nlp.py — добавляет данные, которых нет в скелете.

### 1.1 FileWalker (`enrichment/walker.py`)

- Рекурсивный обход `targets/trainticket/` с исключением `target/`, `node_modules/`, `src/test/`. (P-53, P-05)
- Приоритет: Java → Python → Go → JS/TS.
- Определение сервиса по директории и `pom.xml`.

### 1.2 Парсинг сигнатур (`enrichment/parser.py`)

Из каждого Java-файла:
- `@RequestMapping` / `@*Mapping` → HTTP path, verb
- Аннотации класса: `@RestController`, `@Service`, `@Repository`
- Logger-tags: `logger.info("[Step X] Do Y")` → flow-step токены (P-47, P-48)
- Комментарии-шаги: `// 1. Do X`
- FQN метода: package + class + `#name(paramTypes)` (P-07)

**Не делаем:** полный граф вызовов, DTO-рёбра. Только поисковый контекст.

### 1.3 CodeSignature — привязка к NodeId скелета

```python
@dataclass
class CodeSignature:
    node_id: str          # NodeId из скелета (строго)
    http_path: str        # /api/v1/orderservice/order
    http_verb: str        # POST / GET / ...
    service: str          # ts-order-service
    flow_steps: list[str]  # ["Do Order", "Add new Order"]
    domain_tokens: list[str]  # Token Taxonomy Tier 3 (P-93)
    extra_bm25: str       # logger-tags + комментарии → добавлять к handler_docs()
```

Привязка: FQN из скелета vs FQN из парсера → match.

### 1.4 Расширение BM25 индекса

Добавить `extra_bm25` к документам из baseline `handler_docs()`.  
Это улучшает recall entry point без изменения архитектуры baseline.

**Точка измерения:** % узлов-обработчиков с enriched signature; delta IoU entry-recall на S0.

---

## Фаза 2 — Entry Point Detection (Улучшенный baseline_nlp)

**Цель:** повысить entry point precision/recall vs baseline top-1 per step. (P-106, P-82, P-93)

### 2.1 Step typing (`localization/step_typer.py`)

Keyword matching + параметры из текста:

```python
STEP_KEYWORDS = {
    "login":        ["log in", "login", "sign in"],
    "search_train": ["search", "find train", "trips"],
    "book":         ["book", "order", "reserve"],
    "pay":          ["pay", "payment"],
    "cancel":       ["cancel", "refund"],
    "rebook":       ["rebook", "change seat"],
    "query_food":   ["food store", "browse food"],
    "query_orders": ["refresh", "list of orders"],
    "get_contact":  ["contact book", "first contact"],
    ...
}

PARAMS_FROM_TEXT = {
    "train_type":  r"(high-speed|regular|\bother\b)"  → "G" / "non-G"
    "seat_class":  r"(first-class|second-class)"       → FIRST / SECOND
    "has_food":    r"with food"                        → True
    "has_insurance": r"(?:without|no) assurance"      → False
    "has_luggage": r"(?:without|no) luggage"          → False
}
```

Из `step_type` + params → выбор обработчика при равных BM25 баллах (travel vs travel2, order vs orderOther).

### 2.2 Улучшение `entries_of()` (`localization/searcher.py`)

- Расширить query tokener: добавлять `step_type` как дополнительные токены
- При top-2 с близкими баллами (`travel`/`travel2`, `order`/`orderOther`): брать оба; фильтровать по `train_type` из текста шага (P-75)
- Штраф для admin-обработчиков если сценарий не от администратора (уже в baseline; проверить покрытие)

**Точка измерения:** entry recall@1 на S0 (34 executes → сколько entry найдено верно с `--explain`).

---

## Фаза 3 — BFS с Branch Selection

**Цель:** от entry point по скелету → per-step trace с отсечением паразитных веток.  
**Стартовая точка:** `Index.closure()` из baseline_closure.py — переиспользовать полностью, добавить pruner. (P-54, P-100, P-101, P-102)

### 3.1 Оракул (`trace/oracle.py`) — stateful

```python
@dataclass
class Oracle:
    # Постоянный контекст (из context/README.md)
    account: str = "fdse_microservice"
    balance: int = 10_000
    system_date: date = date(2026, 9, 15)  # system clock
    trip_date: date = date(2026, 10, 1)    # поездки

    # Обновляется между шагами
    train_type: str = ""        # "G" / "D" / "other" — из текста шага
    seat_class: str = ""        # FIRST_CLASS / SECOND_CLASS
    order_status: str = ""      # INIT / PAID / CANCELLED
    has_food: bool = False
    has_insurance: bool = False
    has_luggage: bool = False
    active_services: set[str] = field(default_factory=set)  # сервисы, задействованные в шаге

    def update_from_step(self, step_type: str, params: dict) -> None: ...
    def update_from_trace(self, trace: StepTrace) -> None:
        # INIT → PAID после pay; PAID → CANCELLED после cancel; и т.д.
        ...
```

### 3.2 Branch Pruner (`trace/pruner.py`)

**3a — Детерминированные ветки** (из текста шага, oracle):

| Условие в коде | Источник | Пример |
|---|---|---|
| `trainId.startsWith("G")` | oracle.train_type | "high-speed" → "G" |
| `seatClass == FIRST` | oracle.seat_class | "first-class" в тексте |
| `hasFood == true` | oracle.has_food | "with food" |
| `hasInsurance == true` | oracle.has_insurance | "without assurance" → False |

**3b — Контекстные ветки** (oracle, константы):

| Условие в коде | Значение |
|---|---|
| `account.getBalance() >= price` | True (10 000 > любой цены) |
| `order.getStatus() == PAID` | oracle.order_status == "PAID" |
| `order.getStatus() == INIT` | oracle.order_status == "INIT" |

Если условие не распознано → берём обе ветки (conservative fallback).

### 3.3 Расширенный BFS closure (`trace/closure.py`)

Паттерн из baseline_closure.py с добавлением pruner:

```python
def step_closure(entry: NodeId, oracle: Oracle, idx: Index) -> StepTrace:
    executes, calls, remote, data = set(), set(), set(), set()
    # JWTFilter в executes каждого шага (P-101)
    executes.update(idx.jwt_filters)
    # Основной BFS — как в baseline_closure.Index.closure()
    # + pruner.should_follow(m, t, edge_type, oracle) при calls/remote_call
    # Правило для remote:
    #   активировать только если oracle.is_service_active(target_module)
    # stmt: узлы добавляются в executes (как в baseline submission())
    ...
    return StepTrace(executes, calls, remote, data)
```

**Инварианты:**
- Lombok/reflection методы вне executes/calls (P-107)
- ts-common: module из скелета, не угадывать (P-104)
- ORM data-ребро → к методу с `.findById()`, не к commit (P-103)
- Дочерние таблицы ORM (`user_roles`, `route_stations`, …) — отдельные data-рёбра (P-108)
- call_site → `method` поле (уже в baseline `cs_method[]`)

### 3.4 Агрегация по шагам

```python
def predict(scenario: Scenario) -> Submission:
    oracle = Oracle()
    final = Trace()
    for step in scenario.steps:
        oracle.update_from_step(step_type, params)
        entry = localization.find_entry(step, oracle)
        trace = closure.step_closure(entry, oracle, idx)
        final.merge(trace)            # Union + dedup
        oracle.update_from_trace(trace)
    return final.to_submission(scenario.id)  # с stmt: узлами
```

**Точка измерения:** IoU по S0 (gold доступен); ожидаем overall ≥ 0.55.

---

## Фаза 4 — Семантический слой (Tier 2+)

**Цель:** улучшить entry point поиск через эмбеддинги. Запускать после стабильной Фазы 3.

### 4.1 BGE-M3 Dense поиск

- Замена BM25 в Фазе 2 для сложных случаев.
- LanceDB, `emb_semantic` (~400 токенов/узел). Начать с одного вектора. (P-83)
- Гибрид `alpha×dense + (1−alpha)×BM25`; alpha=0.3 для identifier-запросов. (P-82)
- Seed-эмбеддинги в `.npy` в git. (P-96)

### 4.2 Token Taxonomy offline-индексация (P-93)

Классификация токенов при Enrichment:
- **Системные** (`if`, `return`) → удалить
- **Служебные** (`controller`, `repository`) → `arch_role`
- **Доменные** (`ticket`, `order`, `cancel`) → BM25+Dense профиль
- **Инфраструктурные** (`kafka`, `postgres`) → `infra_tags`

### 4.3 Расширенный Branch Pruner

- Reaching definitions для alias-переменных URL. (P-55, P-56)
- Lightweight type inference для @Autowired интерфейсов. (P-57)

**Точка измерения:** delta IoU Фаза 4 vs Фаза 3.

---

## Зависимости между фазами

```
Фаза 0: baseline_nlp.py воспроизведён (~0.50 на скрытых)
  └─► Фаза 1: Enrichment (code_signatures → расширенный handler_docs)
        └─► Фаза 2: Улучшенный entry point (step_type + enriched BM25)
              └─► Фаза 3: BFS + Oracle + Pruner → submission.json
                    └─► ← IoU измерение на S0; цель ≥ 0.55
                          └─► Фаза 4: BGE-M3 + Token Taxonomy
```

---

## Что переиспользуется из baseline без изменений

| Файл | Компонент | Что берём |
|---|---|---|
| `baseline_closure.py` | `Index.__init__()` | Загрузка скелета, handlers, calls, wiring, module, units, stmts |
| `baseline_closure.py` | `Index.closure()` | BFS с unit-boundary + call_site→метод трансформация |
| `baseline_closure.py` | `Index.submission()` | Формат с stmt: узлами в executes |
| `baseline_closure.py` | `BM25` класс | Переиспользовать как есть |
| `baseline_nlp.py` | `ui_context()` | Парсинг ts-ui-dashboard |
| `baseline_nlp.py` | `handler_docs()` | Два документа: code + UI |
| `baseline_nlp.py` | `entries_of()` | Top-1 BM25 per step; расширить step_type логикой |
| `gold/S0.json` | — | Калибровка (не обучение) |

---

## Что можно отключить без поломки

| Компонент | Флаг | Fallback |
|---|---|---|
| Dense-поиск (BGE-M3) | `--no-dense` | только BM25 (Фаза 2) |
| Branch Pruner | `--no-pruning` | все ветки (как baseline_closure) |
| Enrichment | `--skeleton-only` | entry point только через baseline BM25 |
| remote prediction | `--no-remote` | remote=[] (безопасно для сценариев gold remote=0) |
| Oracle update | `--static-oracle` | константы без update между шагами |

---

## Надбавка за Rust (+10%)

Применяется к итоговому баллу после проверки кода организаторами.  
Тонкая обёртка вокруг Python-решения надбавку **не получает**.  
Рассматривать как опцию только после стабильной работы Фазы 3.
