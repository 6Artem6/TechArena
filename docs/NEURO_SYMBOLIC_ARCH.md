# Neuro-Symbolic Semantic-to-Graph Pipeline

Архитектурный план следующего поколения предиктора.  
Цель: устранить весь hardcode (словари, правила, ручные маппинги) через автоматический вывод из графа + LLM.

---

## 1. Принцип: Zero-Hardcode

В коде предиктора нет доменной логики (поезда, классы мест, ручные списки исключений).  
Вся специфика проекта выводится **оффлайн** через модели и разрешается рантайм-движком через:
- чистую топологию графа
- FSM-состояния, выведенные из кода
- векторную семантику действий и сущностей

---

## 2. Четырёхслойная архитектура

```
[ Слой 1: Макро-семантика ]  Title + Description → Session Entity Mask + Global Prior
            │
            ▼
[ Слой 2: Микро-семантика ]  BGE-M3 Alignment + Qwen Parser → Action-Entity Frame шага
            │
            ▼
[ Слой 3: Кодо-семантика  ]  Три векторных индекса (action / entity / action+entity)
            │
            ▼
[ Слой 4: Символьный solver] FSM из графа + Hub Scorer + Topology Pruning → Финальные рёбра
```

---

## 3. Слой 1 — Макро-семантика (Session Blueprint)

**Вход:** `scenario.title` + `scenario.description`  
**Выход:** Session Entity Mask — набор ключевых сущностей сценария

**Механизм:**
- Qwen парсит title+description в список сущностей: `["RESERVATION", "FOOD_ORDER", "ASSURANCE"]`
- Эти сущности используются как **Global Prior** — гарантированное повышение score для handler'ов с matching entity
- Заменяет: `_TITLE_INJECT_STEP_TYPES` + ручные title-токены в `description_aligner.py`

**Файлы:** `src/localization/description_aligner.py`, `src/localization/qwen_encoder.py`

---

## 4. Слой 2 — Микро-семантика (Action-Entity Frame)

**Вход:** текст шага + aligned description sentence  
**Выход:** универсальный фрейм

```json
{
  "action": "CREATE | READ | UPDATE | DELETE | SEARCH | EXECUTE",
  "entity": "RESERVATION | ORDER | ACCOUNT | CONTACT | FOOD | ...",
  "entity_phrase": "book first-class seat on regular train for contact",
  "modifiers": {"class": "first", "train_type": "OTHER"},
  "constraints": ["with food", "with assurance"]
}
```

**Механизм:**
- Расширение существующего `QwenEncoder.classify()` — новые поля в `StepParams`
- Тот же Qwen-вызов, тот же кеш (ключ по action text)
- Заменяет: ручные extra_tokens в `_build_extra_tokens()` в `step_typer.py`

**Файлы:** `src/localization/qwen_encoder.py`, `src/localization/step_typer.py`

---

## 5. Слой 3 — Кодо-семантика (Dual-Space Vector RAG)

### 5.1 Офлайн: расширение паспортов

В `passport_generator.py` — дополнительное Qwen-поле для каждого handler'а:

```json
{
  "business_summary": "...",
  "synonyms": ["..."],
  "frame": {
    "action": "CREATE",
    "entity": "reservation",
    "entity_phrase": "creates order on the regular train for a contact"
  }
}
```

Изменение: ~20 строк в существующем промпте.  
**Файл:** `src/localization/passport_generator.py`, cache: `cache/service_passports.pkl`

### 5.2 Три индекса

| Индекс | Ключ | Embedding | Использование |
|--------|------|-----------|---------------|
| `action_index` | handler_id | frozenset по `frame.action` | Exact match фильтр |
| `entity_index` | handler_id | BGE-M3(`frame.entity_phrase`) | Семантическое ранжирование |
| `combined_index` | handler_id | BGE-M3(`"{action} {entity_phrase}"`) | Fallback когда одиночные слабо различают |

**Почему BGE-M3 для entity:** Qwen может назвать сущность по-разному в разных контекстах  
(`"BOOKING"` vs `"RESERVATION"` vs `"ORDER"`). BGE-M3 ловит синонимию, exact match нет.

### 5.3 Scoring

```python
# После BM25 + BGE-M3 текущего, перед tau-window:
entity_sim = cosine(step_entity_vec, handler_entity_vec[h])
final_score = bm25_score * (1 + alpha * entity_sim)   # alpha ≈ 0.3
```

Не заменяет BM25 — усиливает совпадения по entity. BGE-M3 уже загружен (`SemanticReranker`).

**Файл:** `src/localization/searcher.py` (init + per-step scoring)

---

## 6. Слой 4 — Символьный solver: FSM из графа

### 6.1 Проблема

Текущий `oracle.py` имеет hardcoded FSM:
```python
# Ручные переходы:
INIT → BOOKED → PAID → COLLECTED
```
И отдельно: `_CARRY_OVER_STEP_TYPES`, `_disambiguate` — ручные правила.

### 6.2 Решение: вывести FSM из skeleton graph

**Граф уже содержит всю информацию:**

```
data edges:
  preserve.createOrder() WRITES → table.orders
  payment.pay()          READS  ← table.orders    → зависимость: RESERVATION before PAYMENT

remote_call edges:
  preserve.preserve() → assurance-service.buyOne()
  preserve.preserve() → contacts-service.getContacts()
                                                   → hub-включение: preserve тянет downstream
```

**Офлайн: Qwen анализирует граф → генерирует `cache/fsm_transitions.json`:**

```json
[
  {
    "from_state": "INIT",
    "to_state": "ORDER_CREATED",
    "trigger_action": "CREATE",
    "trigger_entity": "reservation",
    "entry_handler_pattern": "preserve"
  },
  {
    "from_state": "ORDER_CREATED",
    "to_state": "PAID",
    "trigger_action": "EXECUTE",
    "trigger_entity": "payment",
    "precondition": "ORDER_CREATED"
  }
]
```

**Механизм генерации:**
1. Собрать все `(data_writer, data_reader)` пары из skeleton → зависимости
2. Собрать все `remote_call` с высоким out-degree → hub-правила
3. Qwen prompt: `"Service A writes table X. Service B reads table X. What is A's action+entity and B's precondition?"` → transition
4. Сохранить в cache, Oracle загружает при старте

**Заменяет:**
- Hardcoded `INIT → BOOKED → PAID` в `oracle.py`
- `_CARRY_OVER_STEP_TYPES` — определяется автоматически: шаги с action=CREATE/EXECUTE наследуют контекст
- `excluded_modules` — заменяется hub-правилами: if hub in trace → downstream always included

**Файлы:** новый `scripts/gen_fsm_transitions.py` (офлайн), `src/trace/oracle.py` (загрузка)

### 6.3 G/D disambiguation — топология, не семантика

G/D выбор сервиса остаётся структурным фактом: он выводится из `sibling_metadata.json`  
(суффиксы `-other`, `-2`) — Qwen здесь не нужен. Правило формальное:

```
if module has sibling and effective_train_type is known:
    keep correct side (from sibling_metadata.is_g_side)
```

Не хардкод — структурный вывод из имён модулей в скелете.

---

## 7. Что устраняется

| Hardcode | Файл | Заменяется |
|----------|------|-----------|
| `_build_extra_tokens()` — ручные токены per step_type | `step_typer.py` | Action-Entity frame (Layer 2) |
| `_TITLE_INJECT_STEP_TYPES` | `description_aligner.py` | Session Entity Mask (Layer 1) |
| `excluded_modules` (food, assurance) | `predict.py` | Hub-правила из FSM (Layer 4) |
| `_CARRY_OVER_STEP_TYPES` | `searcher.py` | FSM trigger_action semantics (Layer 4) |
| Hardcoded oracle transitions | `oracle.py` | `fsm_transitions.json` из графа (Layer 4) |
| BM25 doc construction rules | `searcher.py` | `entity_phrase` из паспортов (Layer 3) |

**Не заменяется этим подходом:**
- `_disambiguate` G/D — остаётся, но становится чище (из sibling_metadata)
- hub_scorer gamma — гиперпараметры, не доменная логика

---

## 8. Порядок реализации

| Приоритет | Шаг | Файл | Сложность | IoU impact |
|-----------|-----|------|-----------|------------|
| 1 | Extend passports: добавить `frame` в passport_generator | `passport_generator.py` | Низкая (~20 строк) | Indirect |
| 2 | Extend QwenEncoder: action+entity в StepParams | `qwen_encoder.py`, `step_typer.py` | Низкая (~30 строк) | Indirect |
| 3 | Build entity vectors + scoring в searcher | `searcher.py` | Средняя (~50 строк) | +5-10 executes |
| 4 | gen_fsm_transitions.py — офлайн генерация | новый скрипт | Средняя (~80 строк) | Structural |
| 5 | Oracle: загрузка FSM из JSON | `oracle.py` | Средняя (~40 строк) | Replaces hardcode |
| 6 | Убрать _build_extra_tokens() | `step_typer.py` | Низкая | Cleanup |

---

## 9. Неизменный стек

| Компонент | Роль | Статус |
|-----------|------|--------|
| BGE-M3 (`SemanticReranker`) | Эмбеддинги для entity_index + текущее ранжирование | Переиспользуется |
| Qwen local | Action-Entity extraction + FSM generation | Расширяется |
| BM25 (`ServiceMatcher`) | Первичный retrieval | Остаётся |
| CrossEncoder | Final reranking | Остаётся |
| `pruned_closure` / `enhanced_closure` | BFS expansion по графу | Остаётся |
| `hub_scorer` | Hub boost | Остаётся |
| `sibling_metadata.json` | G/D disambiguation data | Остаётся |
