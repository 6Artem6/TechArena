# Backlog — нереализованные улучшения

Зафиксировано 2026-10-21. Platform best: 0.810630.

---

## B-1. Hub Scorer — подключить `rerank_by_hub` [HIGH IoU-impact]

**Файл:** `src/localization/searcher.py`

`rerank_by_hub` реализован, импортирован, `_hub_reachable` вычисляется каждый вызов.
Один вызов отсутствует → hub-сервисы (preserve, rebook, cancel) не получают boost →
выпадают из tau-window → BFS не расширяется до assurance/contacts/food.

**Добавить после block flow_ctx (до tau-window):**
```python
if _hub_reachable:
    ranked = rerank_by_hub(ranked, _hub_reachable, gamma=0.20, top_k=50)
```

**Ожидаемый эффект:** preserve-service в tau-window → force-swap корректен →
+executes/remote в S2/S3/S6/S7.

---

## B-2. Gap-анализ через diff submission [Диагностика]

Сравнить `scripts/_submission.json` (текущий) с `scripts/_submission_arch132449.json`
(платформенный best pre-SP.1) — посмотреть по сценариям и типам рёбер.

Командой:
```python
python -c "
import json
a = json.load(open('scripts/_submission.json'))
b = json.load(open('scripts/_submission_arch132449.json'))
# diff по каждому сценарию
"
```

Это должно предшествовать любому изменению с неясным IoU-impact.

---

## B-3. Прогрев Qwen-кеша для step_type classification

Qwen cls_v3 кеш пуст (0 записей). step_typer.py при непустом кеше автоматически
переключается на Qwen path вместо regex step_rules — хардкод становится fallback.

**Скрипт:** `scripts/warm_qwen_step_cache.py`
Прогнать все action тексты из scenarios/S*.json через `qwen_encoder.classify()`.

---

## B-4. Enum-геттеры как FP (ts-common:OrderStatus#getCode)

`OrderStatus` — Java enum. Геттеры попадают в trace через calls рёбра.
Фильтр: методы из типов с `kind == "enum"` → infra.

**Файл:** `src/localization/service_classifier.py` → `detect_infra_executes()`

---

## B-5. Oracle FSM outputs не используются

`can_pay()`, `can_cancel()`, `can_rebook()` реализованы — нигде не вызываются.
Подключить для фильтрации нерелевантных сервисов (не предлагать pay если CANCELLED).
