# Open Issues — нерешённые проблемы pipeline

> Дата: 2026-10-21. Best score: 0.768327 (S1=0.807, S2=0.737, S3=0.802, S4=0.808, S5=0.741, S6=0.801, S7=0.683)

---

## I1. Oracle outputs не используются

**Статус:** реализовано, не подключено  
**Файл:** `src/trace/oracle.py`, `src/localization/searcher.py`

Oracle отслеживает статус заказа (INIT → PAID → CANCELLED) и реализует методы:

- `can_pay()` — заказ в статусе INIT
- `can_cancel()` — заказ в статусе PAID
- `can_rebook()` — заказ в статусе PAID

Ни один из этих методов не вызывается в `enhanced_ground`. Результат: pipeline может предложить pay-service для уже оплаченного заказа или cancel-service для несуществующего.

**Что нужно:** в `searcher.py` после построения `hs` — фильтровать handlers чьи сервисы противоречат текущему oracle-состоянию. Например: `oracle.can_pay() == False` → убрать handlers из `ts-inside-payment-service` и `ts-payment-service` из `hs`.

**Риск:** низкий — oracle состояние детерминировано (book → pay → cancel последовательность).

---

## I2. URL-маршруты не используются как имена handlers

**Статус:** не реализовано  
**Файлы:** `src/core/skeleton_index.py`, `src/localization/searcher.py`

В скелете каждый handler имеет `@RequestMapping` аннотацию с REST-путём (`/api/v1/foodservice/createFoodOrder`). Эти пути не индексируются в BM25 и не используются в doc_texts для CrossEncoder.

**Что нужно:** при построении BM25-индекса добавлять токены из URL-пути handler'а. Например, `/api/v1/foodservice/createFoodOrder` → токены `["foodservice", "createfoodorder", "food", "order"]`. Это даёт точное лексическое совпадение для шагов типа "Create food order".

**Как реализовать:** в `passport_generator.py` или `IndexBuilder` добавить извлечение `@RequestMapping` из скелета → append к doc_text handler'а.

---

## I3. Нет настоящего fallback-каскада (BM25 → векторы → LLM)

**Статус:** три уровня применяются всегда, не каскадно  
**Файл:** `src/localization/searcher.py`

Текущий pipeline: BM25 → BGE-M3 rerank → CrossEncoder rerank — все три уровня применяются для каждого шага вне зависимости от качества BM25 результата.

**Чего не хватает:** если BM25 score топ-1 кандидата ниже порога доверия → переключиться на чисто векторный поиск (BGE-M3 без BM25 prior). Если и вектор не уверен → использовать Qwen с расширенным промптом.

**Метрика доверия:** `best_score / median_score` — если ratio близко к 1 (все кандидаты одинаково неуверенные) → BM25 не нашёл ничего конкретного.

**Что нужно:** добавить `confidence = ranked[0][0] / (sum(sc for sc,_ in ranked[:5]) / 5)` после BM25. Если `confidence < CONFIDENCE_THRESHOLD` → запустить pure-vector search через BGE-M3.

---

## I4. Qwen не генерирует теги сервисам

**Статус:** не реализовано  
**Файлы:** `src/localization/qwen_encoder.py`, `src/localization/step_typer.py`

Сейчас: Qwen классифицирует шаг пользователя и возвращает `services[]` с тегами для текущего действия (`food_station`, `insurance`). Но теги для самих микросервисов не генерируются.

Из-за этого сопоставление "шаг → сервис" идёт через BM25 по именам хендлеров, а суффиксы/префиксы убираются хардкодом (`ts-` prefix, `-service` suffix в `step_typer.py`).

**Что нужно:** офлайн-генерация тегов сервисам через Qwen:

```
Prompt: "Given microservice name 'ts-food-service' and its methods 
[getAllFood, createFoodOrder, getFoodStoresByStation], 
generate 3-5 natural language tags describing what this service does."
→ ["food ordering", "station food store", "food delivery"]
```

Теги сохранить в `cache/service_tags.pkl`. В searcher использовать их вместо хардкода для disambiguation. Это устраняет нарушение Zero-Hardcode в `step_typer.py`.

---

## I5. Qwen не генерирует keywords/regex/vectors для поиска

**Статус:** не реализовано (было в планах)  
**Файлы:** `src/localization/qwen_encoder.py`, `src/localization/step_typer.py`

Сейчас `extra_tokens` в `step_typer.py` — хардкодные if-блоки:

```python
if step_type == "cancel":
    tokens.extend(["ticket", "verify"])
if step_type == "rebook":
    tokens.extend(["rebook", "rebook"])
```

**Что нужно:** Qwen генерирует для каждого `step_type` набор bridge-tokens офлайн:

```
Prompt: "For action 'cancel a train ticket', generate 5-7 Java method name 
keywords that would appear in the code handling this action."
→ ["cancelTicket", "cancelOrder", "refund", "ticketStatus", "calculateRefund"]
```

Сохранить в `cache/step_token_bridges.pkl`. `_build_extra_tokens()` читает из кеша вместо if-блоков.

Дополнительно: Qwen может генерировать regex-паттерны для определения step_type из action text — вместо текущего regex-fallback в `classify()`.

---

## I6. condition_text в FSM написан вручную

**Статус:** хардкод в JSON  
**Файл:** `cache/fsm_transitions.json`, `scripts/gen_fsm_transitions.py`

Условия переходов (`"order includes food"`, `"first class seat"`) написаны вручную. Это нарушение Zero-Hardcode — хардкод перенесён из .py в .json.

**Исправление:** `scripts/gen_fsm_conditions.py` уже реализован и делает именно это через Qwen. Большинство condition_text корректны: "order includes food" ✓, "first class seat" ✓, "order requires assurance" ✓.

**Реальная проблема — слабые condition_text для части переходов:**
- `getContactsById` → `"request/order data"` — слишком generic
- `checkSecurity` → `"request/order data"` — аналогично
- `getTripAllDetailInformation` → `""` — пустое

**Что нужно:** перезапустить `gen_fsm_conditions.py` с улучшенным промптом (требовать конкретный бизнес-смысл, не "request/order data"), либо вручную исправить ~10-15 слабых entries.

---

## I7. step_type фильтр в FSM — нарушение Zero-Hardcode

**Статус:** временный хардкод  
**Файл:** `src/localization/fsm_transitions.py:113`

```python
_FSM_STEP_TYPES = frozenset({"book", "rebook"})
```

Это ручной список типов шагов для которых FSM conditions активируются. Нарушает Zero-Hardcode.

**Правильная альтернатива:** активировать FSM conditions только если в `hs` уже есть handler из сервиса-источника перехода (preserve-service/preserve-other-service). Если preserve не попал в hs — его downstream conditions нерелевантны. Это топологический критерий, а не ручной список типов.

---

## Приоритет

| # | Issue | Сложность | Потенциальный эффект |
|---|-------|-----------|----------------------|
| I4 | Qwen теги сервисам | Средняя | Убирает хардкод step_typer |
| I5 | Qwen keywords офлайн | Средняя | Улучшает BM25 для слабых шагов |
| I2 | URL-маршруты | Низкая | TP для REST-ориентированных шагов |
| I6 | condition_text из кода | Высокая | Закрывает архитектурную дыру FSM |
| I1 | Oracle outputs | Низкая | Убирает FP при pay/cancel после смены статуса |
| I3 | Fallback каскад | Средняя | Улучшает слабые шаги без лексики в BM25 |
| I7 | FSM step_type хардкод | Низкая | Рефакторинг, не влияет на score |
