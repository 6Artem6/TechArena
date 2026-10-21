# Priority Plan — Результаты и статус

> Baseline на момент составления плана: **0.768327**  
> Best на момент этого документа: **0.810630**  
> Суммарный прирост за 4 сессии: **+0.042303**

---

## P1. URL-токены в паспортах

**Статус: ВЫПОЛНЕНО** (сессия 2, 2026-10-21)

**Что сделано:**  
Реализовано в `src/predict.py:279-282` — при построении `doc_texts` для BGE-M3/BM25 каждый handler получает строку:
```python
path = "/" + "/".join(tpl) if tpl else ""
base = f"{verb} {path} {name} {class_simple} {module}".strip()
```
URL path из `@RequestMapping` аннотаций включён в base → используется и в BM25, и в BGE-M3 (через `texts[nid] = base + " " + ...`).

**Эффект:** Улучшение entry point accuracy для шагов с явной URL-структурой. Способствовало +S1 0.017 вместе с CE top_k=30.

**Открытый вопрос:** Можно ли повторить path в base дважды (`f"{verb} {path} {path} {name}..."`) для усиления TF в BM25 — ещё не проверено. Риск: может перевесить семантику для BGE-M3.

---

## P2. Layer-метки для фильтрации ts-common шума

**Статус: ЗАКРЫТО как нерабочий подход**

**Что попробовали:**  
- Сессия 2: реализован `_is_shallow_method()` + `_get_non_entry_modules()` в `src/trace/closure.py` — фильтрация leaf-методов из non-entry модулей.
- Результат: регрессия avg 0.770730 → **0.723603 (−0.047)**.

**Почему не работает:**  
ts-common методы (User#getAuthorities, entity конструкторы, DI callbacks) **входят в gold trace** как TP. BFS добраться до них через `calls` рёбра от auth-service, preserve-service — это правильное поведение. Фильтрация "leaf methods in non-entry modules" убирает реальные TP.

**Вывод:**  
Layer-метки как фильтр FP не работает для данного gold. ts-common = бизнес-зависимость (Spring Security UserDetails, entity lifecycle), а не инфра. Подход закрыт.

---

## P3. Oracle outputs подключить

**Статус: НЕ РЕАЛИЗОВАНО**

**Почему отложено:**  
- Oracle FSM (INIT→PAID→CANCELLED) реализован, `can_pay()`/`can_cancel()`/`can_rebook()` доступны в `src/trace/oracle.py`.
- Ожидаемый эффект скромный: best 0.768+ достигнут без него.
- Приоритет упал после того, как другие изменения (is_refused, consign excluded) дали бо́льший эффект.
- Ни в одной сессии не был нужен — платформенный gold не показывает явных противоречий с FSM-состоянием.

**Когда имеет смысл:**  
Если появятся сценарии где cancel-методы предсказываются до оплаты (order_status=INIT) или pay-методы после отмены — Oracle сможет убрать эти FP.

---

## P4. Fallback при пустом ranked

**Статус: НЕ РЕАЛИЗОВАНО**

**Почему отложено:**  
За три сессии не встречалось шагов где ranked оказался бы пустым — BM25+BGE-M3 всегда возвращает кандидатов. Fallback актуален только для экзотических шагов. Оставлен как low-priority.

---

## Шаги вне PRIORITY_PLAN (хронологически)

### Сессия 2 (2026-10-21, +0.018803 от 0.768327→0.787304)

| Изменение | Файл | Эффект |
|-----------|------|--------|
| `is_refused` filter — "the system refuses" → hs=set() | `step_typer.py`, `searcher.py` | **S7: 0.683→0.799 (+0.116)** |
| `step_rules` reorder — update_consign перед query_consign | `src/domain_config.yaml` | Исправлен false-match "consignment record" |
| CE `top_k=30` (было 20) | `searcher.py` | +S1 0.017 |
| `extra_tokens` split: add/update/query_consign раздельные токены | `step_typer.py` | Disambiguates insert vs update vs query |

**Попытки без успеха:**
- `is_refused` transit closure train_type filter → регрессия S5 0.741→0.693 (откат)
- entries post-filter by train_type → нет эффекта (откат)
- continuity sibling filter → нет эффекта (откат)

---

### Сессия 3 (2026-10-21, +0.016357 от 0.787304→0.803661)

| Изменение | Файл | Эффект |
|-----------|------|--------|
| **consign excluded_modules** — ts-consign-service в excluded при `not _has_consign` | `src/predict.py` | **S2: 0.737→0.762, S4: 0.808→0.836, S5: 0.741→0.763, S6: 0.801→0.831** |
| oracle `_HAS_LUG` расширен: `a\s+luggage\s+consignment` | `src/trace/oracle.py` | S3 корректно определяет has_luggage=True из step 6 |
| oracle `_NO_LUG` добавлен: `no/without luggage` | `src/trace/oracle.py` | Не-False-positive при "no luggage consignment" |
| `query_consign + "account"` extra_token | `src/localization/step_typer.py` | S0 local: findByAccountId вместо findByConsignee (IoU 0.871→1.0) |
| **zero-calls handlers filter** — home()/getHello() post-BFS убраны | `src/predict.py` | +0.001299 avg по всем сценариям |

**Диагностические находки без изменений:**
- ts-payment-service во всех сценариях = TP (inside-payment.pay → remote_call → payment.pay)
- ts-station-food-service: getFoodStoresOfStation vs getFoodListByStationFoodStoreId — NLP disambiguation, без gold не исправить
- S5 ts-order-service (G, 11 методов) при D-train = gold TP через transit closure

---

## Итоговая таблица сценариев

| Сценарий | Start (plan) | Сессия 2 | Сессия 3 | Сессия 4 | Delta |
|----------|-------------|----------|----------|----------|-------|
| S1 | 0.807 | 0.824 | 0.826 | 0.837 | +0.030 |
| S2 | 0.737 | 0.737 | 0.763 | 0.768 | +0.031 |
| S3 | 0.802 | 0.802 | 0.803 | 0.809 | +0.007 |
| S4 | 0.808 | 0.808 | 0.838 | 0.848 | +0.040 |
| S5 | 0.741 | 0.741 | 0.764 | 0.768 | +0.027 |
| S6 | 0.801 | 0.801 | 0.832 | 0.838 | +0.037 |
| S7 | 0.683 | 0.799 | 0.800 | 0.806 | +0.123 |
| **avg** | **0.768** | **0.787** | **0.804** | **0.811** | **+0.043** |

---

## Сессия 4 (2026-10-21) — нейтральный результат

**Попытки без прироста:**
- Паспорта добавлены для preserve#preserve, travel×3, order-other×3 (cache 240→250) → нейтрально
- `get_contact` extra_tokens ["account","account"] → нейтрально
- `enter_station`/`collect` extra_tokens → нейтрально  
- executeTicket паспорт → "enter station", "board train" synonyms → нейтрально

**Ключевой вывод:** все entry points уже находятся правильно через существующие механизмы. Паспорта и extra_tokens влияют только при disambiguation — если winner уже правильный, эффекта нет.

**Структурный анализ S2 vs S1:** S2 правильно исключает assurance/consign (нет luggage/insurance), включает cancel/rebook/order-other/preserve-other — архитектура правильная. Разница в IoU (S1=0.826 vs S2=0.763) — вероятно в gold trace, не в FP.

---

## Сессия 6 (2026-10-21) — remote zero-calls filter

**Best: 0.810630 (+0.006969)**

| Изменение | Файл | Эффект |
|-----------|------|--------|
| **remote→home() FP fix**: `remote = {(s,t) for s,t in remote if t not in _zero_calls}` | `src/predict.py:227` | **+0.007 avg, все S1-S7 улучшились** |

**Root cause**: `_zero_calls` фильтровал только `edges` (calls) но не `remote`. Zero-call HTTP handlers (home(), getHello()) получали remote-рёбра от других сервисов через skeleton remote_call edges. Убирание FP remote→home() дало системный прирост по всем сценариям (1-2 FP на сценарий).

---

## Сессия 5 (2026-10-21) — Г6 нейтрально, station food регрессия

**Best: 0.803661 (был)**

**Попытки без прироста / с регрессией:**

| Изменение | Файл | Эффект |
|-----------|------|--------|
| CRUD-нормализация: убрать "get" из handler name doc_text | `ui_context.py` | **Нейтрально** — нулевой diff во всех S0-S7. BM25 ranking не меняется. |
| Г5 (Verb tokens): добавить HTTP verb в extra_tokens | `ui_context.py`, `step_typer.py` | **Регрессия S0: 0.980→0.925** (откат, из предыдущей сессии) |
| Station food: split has_food_train/has_food_station — исключить ts-food-service при station food | `oracle.py`, `predict.py` | **Регрессия: 0.803661→0.794651 (−0.009)**, откат. Gold S2/S3 включает ts-food-service даже при "station food"! |

**Ключевые выводы:**

1. **"get" токен нейтрален**: 9.7% handlers имеют "get" в имени, IDF≈1.25, но убирание из doc_text не влияет на rankings — BM25 результаты не чувствительны к этому токену.

2. **Station food ≠ train food в gold**: Несмотря на логику "food from a station store" → preserve-other → food-service chain всё равно присутствует в gold. Вероятно, preserve-service всегда вызывает ts-food-service при has_food=True независимо от типа источника еды.

3. **Граф-алгоритмы (Dijkstra/A*/PageRank) не помогут**: наш bottleneck — качество Entry Point selection (BM25 Level 1), а не алгоритм обхода. DNS-воронка уже реализована (BM25 → BGE-M3 → BFS). Граф не организован по семантической близости.

## Следующие кандидаты

1. **CFG branch pruning** — `if (tripId.startsWith("G"))` через tree-sitter. Единственное что может дать системный прирост без gold. Конкретно: найти все conditional branches в Java где условие зависит от train_type → при D-train пропускать G-ветку.
2. **S2/S5 FP анализ** — нужен gold или платформенный feedback на уровне edge types.
3. **Конфликт имён UserController.java** — ts-user-service паспорта не загружаются (два файла с одним именем).
