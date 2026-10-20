# Semantic-to-FSM Pipeline: архитектурное предложение

## Проблема

Текущий пайплайн (BM25 + BGE-M3 + hub_scorer) работает с запросом как единым текстом. Это создаёт три системных проблемы:

**1. Семантическое перекрытие Action vs. Condition**

Запрос «Book a first-class seat for the trip, with food and assurance» содержит:
- **Action** (ядро): `create_booking`
- **Condition** (контекст): `seat_class=FIRST`, `has_food=True`, `has_insurance=True`

BGE-M3 смешивает эти уровни. Токены «food», «assurance» усиливают ts-food-service и ts-assurance-service в BM25 top-k → hub_scorer для ts-rebook-service растёт, т.к. он тоже может достичь этих сервисов → ts-rebook-service попадает в tau-окно для `book` шагов.

**2. Каскадное разрушение от неверного хаба**

Выбранный неверный hub-сервис (ts-rebook-service вместо ts-preserve-service) попадает в `oracle.active_services` → continuity механизм добавляет его для всех последующих шагов → BFS из rebook entry добавляет 16 методов во весь сценарий.

**3. Хрупкость хардкодированных компенсаций**

Текущий fix: `gamma=0.10 for book, gamma=0.16 for rebook`. Это параметр-патч, который:
- Не работает для S3/S5 (food+insurance extra_tokens повышают rebook hub score)
- Не переносится на другие проекты
- Ломается при изменении service passports

---

## Предлагаемая архитектура: Action-Condition Decomposition

### Идея

Разделить запрос И сервисные возможности на два ортогональных измерения перед поиском:

```
Запрос → [Action Extractor] → action_key    ─→ [Action Index Search] → кандидаты
        → [Condition Extractor] → conditions  ─→ [Condition Re-ranking] → финальный список
```

### Уровень 1: Оффлайн — FSM Service Passport

Расширить текущие service passports (`cache/service_passports.pkl`) двумя полями на каждый метод:

```json
{
  "PreserveController/preserve": {
    "action_summary": "Create a new train ticket reservation for a passenger",
    "action_embedding": [...],          // BGE-M3 вектор action_summary
    "condition_summary": "Requires: active trip, valid contact, optional food/insurance/consignment",
    "condition_embedding": [...],       // BGE-M3 вектор условий
    "guard_states": ["has_trip_id", "has_contact", "train_status=AVAILABLE"]
  },
  "RebookController/rebook": {
    "action_summary": "Change an existing paid booking to a different trip or seat class",
    "action_embedding": [...],
    "condition_summary": "Requires: existing order in PAID or INIT status, target trip available",
    "condition_embedding": [...],
    "guard_states": ["order_status=PAID", "target_trip_available"]
  }
}
```

**Генерация**: Qwen prompt с явным требованием разделить action от conditions. Единоразово, оффлайн, результат в `cache/`.

### Уровень 2: Runtime — двухфазный поиск

**Фаза 1: Action Match**
```
step_text → Qwen decompose → action_key (e.g. "create_new_booking")
action_key → BGE-M3 embed → search action_index → top-30 кандидатов
```

**Фаза 2: Condition Re-ranking**
```
oracle_state (train_type, has_food, ...) + step_text
→ condition_query  
→ BGE-M3 embed → rank top-30 по condition_index
→ финальный список handler-ов
```

**Результат**: ts-preserve-service для "create_new_booking" с condition train_type=G → top-1. ts-rebook-service для "change_existing_booking" → top-1 only when step=rebook.

### Уровень 3: Топологический Solver (существующий)

hub_scorer + pruned_closure + sibling force-swap работают как сейчас, но получают ЧИСТЫЙ список entry points от Action-Condition engine.

---

## Декомпозиция запроса: FSM-язык

### Шаг → FSM-событие

```
"Book a first-class seat on the high-speed trip for this contact, 
 with food from a station store, assurance of the first type and luggage consignment"
```

Декомпозиция:
- **action**: `BOOK_TICKET`
- **target**: `{trip_type: "G", seat_class: "FIRST"}`
- **services**: `[food_station, insurance=TYPE_1, consignment={weight: ?}]`
- **actor**: `passenger` (implied)
- **guard**: `{contact: selected, trip: selected}`

Это язык FSM из challenge description. Не нужно привязываться к code-специфике — декомпозиция ведётся на уровне бизнес-действий.

### Сопоставление с сервисами

После декомпозиции каждый аспект матчится независимо:
- `action: BOOK_TICKET` → search action_index → ts-preserve-service (preserve handler)
- `services: food_station` → search action_index → ts-station-food-service
- `services: insurance` → search condition_index → ts-assurance-service (если `has_insurance=True`)

Это позволяет контролировать каждый аспект, не смешивая их в один вектор.

---

## Практическая реализация

### Шаг 1: Расширить Qwen passport prompt

Текущий prompt в `src/localization/passport_generator.py` генерирует `business_summary` + `synonyms`. Расширить на:

```python
ACTION_CONDITION_PROMPT = """
For Java method `{method}` in `{class}`:

Respond as JSON:
{
  "action_summary": "One sentence: WHAT this method does conceptually (verb + object, e.g. 'creates a new train reservation')",
  "condition_summary": "One sentence: WHEN this method is called and WHAT prerequisites it requires",
  "guard_states": ["list", "of", "required_conditions"]
}

Do NOT include implementation details. Focus on business semantics.
"""
```

### Шаг 2: Двухиндексный LanceDB (или in-memory numpy)

Для каждого HTTP handler хранить два embedding вектора. При поиске:
1. Encode action_query → cosine search action_embs → top-30
2. Encode condition_query (из oracle state) → re-rank top-30 по condition_embs

### Шаг 3: Интеграция в enhanced_ground

Заменить текущий блок hub_scorer для book шагов на action-condition search:

```python
if step_info.step_type in ORCHESTRATOR_STEPS:
    # Action query: ядро действия без условий
    action_q = decompose_action(act)
    # Condition query: из oracle state
    condition_q = build_condition_query(oracle, step_info)
    
    ranked = action_condition_search(
        action_q, condition_q,
        ranked, _action_embs, _condition_embs,
        gamma_action=0.6, gamma_condition=0.4
    )
```

---

## Что это решает (конкретно)

| Проблема | Текущий фикс | Action-Condition фикс |
|----------|-------------|----------------------|
| book vs rebook FP | gamma tuning (хрупко) | Разные action_embeddings для "create" vs "change" |
| preserve G vs OTHER | sibling swap (работает) | Condition: `train_type=G/D` в condition_query |
| assurance missing | oracle excluded_modules | Condition: `has_insurance=True/False` → include/exclude |
| contacts missing | нет фикса | Action: "open contact book" → contacts-service |
| station-food wrong method | нет фикса | Condition: "food at store" vs "list stores" → разные condition_embeddings |

---

## Связанные файлы

- `src/localization/passport_generator.py` — расширить генерацию паспортов
- `src/localization/searcher.py` — интегрировать action-condition search
- `src/localization/service_profiles.py` — возможно, объединить с новым форматом паспортов
- `cache/service_passports.pkl` — расширить новыми полями (action/condition embeddings)

---

## Статус

### Реализованные компоненты (2026-10-19)

**`src/localization/action_condition.py`** — core модуль:
- `HandlerStepProfile` + `build_handler_profiles(ix)`: URL path → step_type affinity из skeleton
- `fsm_condition_rerank()`: штрафует handlers с несовместимым oracle-состоянием
- `SubIntent` + `extract_sub_intents()`: multi-intent декомпозиция шага
- `DecomposedStep` + `decompose_step()`: полная декомпозиция action + oracle conditions

**Интеграция в `src/localization/searcher.py`**:
- FSM reranking применяется ко всем шагам где oracle доступен
- Только handlers с explicit step_type precondition получают штраф
- preserve/book-affinity handlers: precondition="always" → без штрафа (preserve вызывается из rebook)

**Fix `src/localization/step_typer.py`**:
- "get_contact", "add_contact" добавлены в `_SAFE_KEYWORD_TYPES`
- extra_tokens ["contacts", "contact"] для contacts-шагов

### Эффект (oct 19):

| Сценарий | Было | Стало | Δ | Объяснение |
|----------|------|-------|---|------------|
| S3 | 190 | 167 | -23 | Убраны FP: rebook+preserve-other для G-train |
| S5 | 233 | 210 | -23 | Убраны FP: food/assurance в "without food" сценарии |
| S6 | 185 | 185 | 0 | Без изменений |

Снижение счётчика = улучшение IoU (были ложные положительные).

### Открытые задачи

- [ ] Offline action/condition embeddings через Qwen расширенный passport prompt
- [ ] Двухиндексный поиск action_index + condition_reranker  
- [ ] station-food disambiguation (getFoodListByStationFoodStoreId vs listFoodStoresByStationName)
- [ ] contacts method disambiguation (findContactsByAccountId vs findContactsById)

**Приоритет**: HIGH — решает корневую причину 5+ несвязанных проблем без параметрических хаков.

---

## Session Profile Architecture: оценка и корректировки (2026-10-19)

### Исходное предложение (краткая схема)

Использовать `title` + `description` сценария как **активный контекстный слой** вместо одноразовой admin-детекции.

Три слоя декомпозиции:
1. **Aspect Vector Set** — три ортогональных BGE-M3 вектора: `v_global_intent`, `v_domain_features`, `v_actor_context`
2. **Session Attributes** — Qwen JSON-извлечение: `actor_role`, `is_multi_item`, `expected_entities` (passenger_count, has_station_food, has_assurance, has_consignment, payment_method), `search_criteria`, `order_flow`
3. **Sparse Tokens / BM25** — глобальные токены из title в query каждого шага

Механизм инъекции:
- `V_eff = α * BGE(step) + (1-α) * v_global_intent` (α=0.7)
- BM25 расширение: `step.action + " " + " ".join(global_bm25_tokens)`
- Topological Guard: guaranteed presence для сервисов из `expected_entities`

---

### Оценка: что верно

**Слой 2 — Session Attributes (Qwen extraction)**: самая практичная часть.

Прямо сейчас `title` используется только для `is_admin` через `ADMIN & set(toks(_title_desc))`. Это выбрасывает богатый контекст:
- `has_station_food`, `has_assurance` → oracle стартует «слепым», узнаёт атрибуты только из шагов бронирования
- `order_flow: "multi_booking_batch_payment"` — уникальная сигнатура, которую per-step oracle НИКОГДА не узнает заранее
- `is_multi_item: true` + `passenger_count: 2` — нет в шагах явно, только в title

**Ключевой недостающий механизм: Oracle Pre-loading из title**

До первого шага у oracle нет информации. Это создаёт проблему: step 0 (login) и step 1 (search) выполняются с `has_food=None`, `train_type=""`. Когда title явно говорит "with food on the high-speed train", oracle мог бы знать это с самого начала — что влияет на extra_tokens шагов поиска.

Добавить: `oracle.preload_from_title(title + " " + description)` — вызвать один раз в `enhanced_ground` до цикла по шагам, используя `step_typer.classify()` как источник атрибутов.

**`order_flow` и continuity**: текущий oracle не знает заранее, что будет 2 итерации preserve+pay. Это приводит к потере second-order-service на поздних шагах, если continuity window закрылась. Структурная метаинформация из title помогает.

---

### Оценка: что требует правки

**Проблема 1: Universal BM25 injection = FP в login/search шагах**

```python
query_bm25 = step.action + " " + " ".join(global_bm25_extra_tokens)  # ДЛЯ ВСЕХ шагов
```

Для step 0 "Log in with username and password" + `["food", "assurance", "consignment"]`:
- BM25 score ts-food-service растёт → попадает в tau-окно для login шага
- Это именно та проблема, которую FSM reranking только что исправил (S5: 233→210)

**Правильно:** инъекция только когда:
```python
step_info.step_type in {"book", "pay", "rebook", "view_trip", "query_insurance", "query_consign"}
AND global_attr соответствует токену (has_station_food → inject "station food")
```

**Проблема 2: "Guaranteed presence" конфликтует с per-step oracle**

Если `has_station_food=True` из title, но step 7: "Book a second-class seat, **without food, assurance** or consignment":
- Guaranteed presence → station-food-service включён принудительно
- Но step явно говорит "without food" → oracle.has_food=False → должен ИСКЛЮЧИТЬ

Приоритет: **per-step oracle state всегда перекрывает session-level preload**. Title даёт только начальное значение для oracle, не финальное.

**Проблема 3: Dense fusion с фиксированным α**

`V_eff = 0.7 * BGE(action) + 0.3 * v_global_intent` — одинаково для всех step_type.

Для `search_train` шагов `v_global` = "book two tickets with food" тянет вектор в сторону preserve-service, именно то что FSM reranking убирает. α должен зависеть от step_type:

| step_type | α (local weight) | Обоснование |
|-----------|-----------------|-------------|
| login/register | 1.0 | Глобальный контекст нерелевантен |
| search_train/advanced | 0.95 | Global не должен буcтить preserve |
| book/rebook | 0.70 | Смесь полезна |
| pay/collect | 0.65 | Global flow помогает |
| view_trip | 0.80 | Умеренный mix |

**Проблема 4: `v_actor_context` как вектор — избыточно**

Роль actor бинарная (passenger/admin). Текущий `ADMIN & set(toks(_title_desc))` справляется. Нет ROI от отдельного вектора.

---

### Дополнения к предложению

**Добавить: Pre-loading oracle из title (критично)**

```python
# В enhanced_ground, до цикла:
if oracle is not None and title:
    _pre = step_typer.classify(title + " " + description)
    oracle.preload(
        train_type = _pre.train_type or oracle.train_type,
        has_food = _pre.has_food,          # None → не перезаписывать
        has_insurance = _pre.has_insurance,
        has_luggage = _pre.has_luggage,
    )
```

Требует добавить `oracle.preload()` — метод, устанавливающий только non-None поля.

**Добавить: `expected_order_count` из title**

Из "paying for both orders" / "two passengers" → `expected_order_count=2`. Continuity-механизм может использовать его для удержания second-order-service в active_services.

**Добавить: BM25 token injection как step-gated дополнение к extra_tokens**

```python
# В _build_extra_tokens():
if session_profile and step_info.step_type in PROFILE_INJECT_STEPS:
    if session_profile.has_station_food and step_info.step_type == "book":
        tokens.extend(["station", "food", "store"])
    if session_profile.has_assurance and step_info.step_type == "book":
        tokens.extend(["assurance", "insurance"])
```

Это точнее, чем global BM25, и интегрируется в существующий `_build_extra_tokens` pipeline.

**Добавить: Scope validation (a-posteriori, опционально)**

После обработки всех шагов сравнить `expected_entities` с финальным prediction. Если `has_station_food=True` из title, но station-food-service отсутствует → warning + second-pass поиск с boosted query. Низкий приоритет, но полезно для диагностики.

---

### Итоговая рекомендация по реализации

Реализовывать в порядке ROI:

| Шаг | Механизм | Файл | Ожидаемый эффект |
|-----|----------|------|-----------------|
| 1 | Oracle pre-loading из title | `oracle.py` + `searcher.py` | Защита от "blind" первых шагов |
| 2 | Session Attributes (Qwen) | новый `session_profile.py` | `order_flow`, `actor_role`, `expected_entities` |
| 3 | Gated BM25 injection | `step_typer.py` | Точный boost без global FP |
| 4 | Step-type-aware α | `searcher.py` | Предотвратить глобальный drift |
| ❌ | Universal BM25 injection | — | Не реализовывать: вызывает FP |
| ❌ | Guaranteed presence | — | Не реализовывать: конфликтует с per-step oracle |
| ❌ | `v_actor_context` вектор | — | Избыточно поверх текущего admin-детектора |

**Текущий приоритет перед Session Profile**: закрыть открытые задачи FSM (offline embeddings, station-food disambiguation) — они дадут более прямой IoU-прирост.
