# FSM + Qwen Service-Routing Architecture

**Статус**: plan / обсуждение  
**Цель**: > 0.768330 (текущий best)  
**Дата**: 2026-10-20

---

## Проблема, которую решаем

Текущий pipeline находит entry-handlers через BM25 + BGE-M3 + CrossEncoder по тексту шага.
Это хорошо работает для уникальных сервисов, но ломается в двух случаях:

1. **Hub-сервисы** (preserve, rebook) — их методы называются "create", "modify" и не содержат
   бизнес-терминологии из action-текста → BM25 их не находит → whole downstream chain пропадает.

2. **Sibling disambiguation** (preserve vs preserve-other) — force-swap срабатывает только если
   G-side уже есть в tau-window, но если BM25 не нашёл preserve вообще — замена невозможна.

Результат: в S2/S3 отсутствует ts-preserve-other-service + 10 downstream сервисов → -29 executes.

**Архитектурное решение**: разделить "найти entry service" (маршрутизация) и "развернуть граф"
(BFS). Маршрутизация — Qwen с семантическими описаниями. BFS — предвычисленный граф.

---

## Архитектура: два уровня

### Уровень 1 (offline) — Closure Table

```
Для каждого handler H в индексе:
    BFS(H, train_type=G) → frozenset[method_nids]  # G-train граф
    BFS(H, train_type=D) → frozenset[method_nids]  # D-train (Other) граф
    BFS(H, train_type="") → frozenset[method_nids] # transit (cross-type)

Сохранить в cache/closure_table.pkl
```

- Запускается один раз, ~5 мин (итерация по всем handlers)
- Уже частично сделано в `hub_scorer.precompute_reachability` — но там только H→H (handler-to-handler),
  нужно H → все методы
- На запросе: `closure_table[(handler_nid, train_type)]` → мгновенный lookup

### Уровень 2 (online) — Service Routing через Qwen + Bi-encoder

Для каждого шага (action-текст):

```
Step 1: BGE-M3 similarity(action) vs service_profile.vector_search_phrases
        → top-5 candidate services

Step 2: Qwen prompt:
        "Given booking step: '{action}' (step type: {step_type}),
         which service(s) handle this operation?
         Candidates:
           1. ts-preserve-service: {description from service_profiles}
           2. ts-preserve-other-service: {description}
           ...
         Reply with service name(s)."

Step 3: Resolve to handler nids → lookup closure_table → union of closures
```

**Ключевой принцип (из запроса пользователя)**: Qwen никогда не видит голые opaque имена
типа "ts-preserve-other-service". Она видит описания из `service_profiles_v1.pkl`:
- `ts-preserve-service`: "Books and reserves train tickets including food and insurance options..."
- `ts-preserve-other-service`: "Books Other/K/T/Z-category train tickets with same downstream chain..."

---

## Варианты подачи сервисов в Qwen

### Вариант A (bi-encoder pre-selection)

```
action_vec = BGE-M3.encode(action)
for svc in all_services:
    scores[svc] = cosine(action_vec, svc.profile_vec)
top_k = top_5(scores)                          # сокращаем до 5
→ Qwen выбирает из 5 с описаниями
```

Плюсы: Qwen видит только 5 кандидатов → меньше confusion, быстрее промпт.  
Риск: если правильный сервис не попал в top-5 → miss.

### Вариант B (полный список с описаниями)

```
→ Qwen видит все 42 сервиса с описаниями
```

Плюсы: нет риска early cutoff.  
Риск: длинный промпт, Qwen может запутаться между похожими сервисами.

### Вариант C (гибрид — рекомендуется)

```
top_30 = bi-encoder top-30 (широкий фильтр)  # убирает явно нерелевантные
→ Qwen выбирает из 30 с описаниями
```

Bi-encoder как фильтр шума, Qwen как точный выбор. top-30 гарантирует, что preserve-other
(сестринский сервис) попадёт рядом с preserve.

---

## Описания сервисов — источник данных

`cache/service_profiles_v1.pkl` — 42 сервиса, каждый содержит:
- `one_word_tag`: "preserve", "order", "rebook" ...
- `bm25_keywords`: ['send', 'preserve', 'dipatch', 'seat']
- `vector_search_phrases`: ['Books and reserves a train ticket...', ...]

`cache/service_passports.pkl` — per-method:
- `business_summary`: "Create a ticket reservation with seat assignment and insurance..."
- `synonyms`: ["book ticket", "reserve seat", "purchase train"]

Для промпта Qwen использовать `vector_search_phrases[0]` из service_profiles как краткое
описание сервиса. Для точного handler-level routing — business_summary из passports.

---

## Риски

| Риск | Вероятность | Влияние | Митигация |
|------|-------------|---------|-----------|
| Qwen выбирает не тот сервис из похожих | Средняя | Высокое | Вариант C (top-30 bi-encoder pre-filter) |
| Многосервисные шаги (book → 10+ сервисов) | Высокая | Высокое | Qwen возвращает список; union closures |
| Qwen не знает G/D-сторону для sibling пар | Высокая | Высокое | После выбора сервиса: force-swap по oracle.train_type |
| Closure table не покрывает редкие handlers | Низкая | Среднее | Fallback: BFS on-the-fly |
| Латентность Qwen на всех шагах | Высокая | Среднее | Кеш по (step_type, action_hash); batch mode |
| Passport descriptions только "home" endpoints | Высокая | Среднее | Использовать service_profiles вместо passports |
| Service profiles не покрывают transit логику | Средняя | Среднее | Transit entries остаются через текущий BM25 |

### Критический риск: multi-service шаги

Шаг "book" вызывает: preserve → travel, order, assurance, consign, contacts, food, seat,
security, station, user (10+ сервисов). Qwen должна вернуть "ts-preserve-service" (entry),
а не все 10 downstream. Downstream получаем через BFS из closure table.

Если Qwen вернёт downstream вместо entry — получим wrong BFS root → incomplete trace.
**Митигация**: в промпте явно указать "return the ENTRY service that the user directly calls,
not the downstream dependencies."

### Критический риск: sibling пары

Qwen видит описание ts-preserve-service и ts-preserve-other-service — они семантически почти
одинаковы (оба бронируют билеты). Qwen не может знать, какой нужен без знания train_type.

**Митигация**: Qwen возвращает `preserve` (generic) → pipeline применяет force-swap по
oracle.train_type после: G → preserve-service, D → preserve-other-service.

Промпт: "If both ts-preserve-service and ts-preserve-other-service apply, return 'preserve'."

---

## Связь с текущим disconnected hub_scorer

Шаг 1 плана (из IMPROVEMENTS.md): подключить `rerank_by_hub` — это другой механизм с той же
целью. Оба можно использовать:

- **hub_scorer** (краткосрочно, ~1 строка): boost hub-сервисов в BM25 ranking → preserve
  входит в tau-window → force-swap работает. Риск: gamma слишком высокий → FP.

- **FSM+Qwen** (среднесрочно, ~2-3 дня): явная маршрутизация через Qwen → более точно,
  не зависит от BM25 tau-окна. Риск: латентность, Qwen ошибки.

**Рекомендация**: сначала hub_scorer (быстро, низкий риск), потом FSM+Qwen поверх.

---

## Фазы реализации

### Фаза 0 (сейчас, ~30 мин) — подключить hub_scorer

**Файл**: `src/localization/searcher.py`  
**Место**: после вычисления `_hub_reachable` (line ~663), перед tau-window  
**Изменение**: добавить вызов `rerank_by_hub(ranked, _hub_reachable, gamma=0.20)`

Ожидаемый эффект: preserve входит в tau-window → S2/S3 +25 executes.  
Верификация: `python scripts/pack.py` → S2 exec ≈ 216 (archive), S3 ≈ 184.

### Фаза 1 (~2-4 часа) — Closure Table

```python
# scripts/precompute_closures.py
from src.localization.indexing import load_index
from src.trace.closure import pruned_closure

ix = load_index("cache/")
table = {}
for handler_nid, handler in ix.handlers.items():
    for train_type in ["G", "D", ""]:
        methods, edges, remote, data = pruned_closure({handler_nid}, ix, train_type)
        table[(handler_nid, train_type)] = {
            "methods": frozenset(methods),
            "edges": frozenset(edges),
            "remote": frozenset(remote),
            "data": frozenset(data),
        }
import pickle
with open("cache/closure_table.pkl", "wb") as f:
    pickle.dump(table, f)
```

Запустить один раз. Lookup O(1) на запрос.

### Фаза 2 (~4-6 часов) — Service Routing (Вариант C)

```python
# src/localization/service_router.py
def route_step_to_services(action: str, step_type: str, profiles: dict,
                            encoder, top_k: int = 30) -> list[str]:
    """BGE-M3 pre-filter → Qwen service selection."""
    # 1. Bi-encoder similarity
    action_vec = encoder.encode(action)
    scores = {svc: cosine(action_vec, prof.profile_vec) for svc, prof in profiles.items()}
    candidates = sorted(scores, key=scores.get, reverse=True)[:top_k]

    # 2. Build Qwen prompt with descriptions
    candidate_list = "\n".join(
        f"  - {svc}: {profiles[svc].vector_search_phrases[0]}"
        for svc in candidates
    )
    prompt = SERVICE_ROUTING_PROMPT.format(
        action=action, step_type=step_type, candidates=candidate_list
    )

    # 3. Qwen call (cached by action hash)
    response = qwen_call(prompt)
    return parse_service_names(response, candidates)
```

### Фаза 3 (~2-4 часа) — Интеграция в predict.py

Заменить `enhanced_ground` для "book"-like шагов:
1. `route_step_to_services` → entry service names
2. Resolve names → handler nids
3. `closure_table[(handler_nid, train_type)]` → union closures
4. sibling force-swap по oracle.train_type
5. Merge с обычным BM25 результатом (union, не замена — fallback при ошибке Qwen)

---

## Ожидаемый эффект по сценариям

| Сценарий | Текущая проблема | Ожидаемое исправление |
|----------|-----------------|----------------------|
| S2 (-29 exec) | preserve-other отсутствует | Qwen видит "Other train booking" → preserve-other → +downstream |
| S3 (-13 exec) | Та же проблема | Аналогично S2 |
| S6 (+5 FP exec) | admin-route getAllRoutes лишние | Routing исключит admin сервисы для passenger шагов |
| S7 (-3 exec FN) | order-other методы | Qwen выберет order-other для "D-train order query" |

Консервативная оценка после Фазы 0 (hub_scorer): +0.020-0.030 к avg IoU.  
После Фаз 1-3 (FSM+Qwen): +0.040-0.060 потенциально.

---

## Что НЕ входит в этот план

- Изменение логики BFS/pruned_closure (не трогать — transit "" доказано важен)
- Хардкоды step_type → service маппингов (Zero-Hardcode Manifesto)
- Изменение sibling_metadata.json (корректен)
- Изменение oracle train_type логики (работает)

---

## Следующий шаг

Обсудить и подтвердить подход → начать с **Фазы 0** (подключить hub_scorer, ~1 строка).
Это быстрая проверка гипотезы о preserve без риска для других сценариев.
