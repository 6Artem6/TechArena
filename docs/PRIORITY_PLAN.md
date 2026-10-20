# Priority Plan — следующие шаги

> Best: 0.768327. Цель: улучшить Precision (убрать FP из ts-common) и Recall (fallback для слабых шагов).

---

## P1. URL-токены в паспортах (быстро, низкий риск)

**Файл:** `src/localization/passport_generator.py`

В doc_text каждого контроллер-хэндлера добавить `@RequestMapping` пути из скелета.

```
Было:  "OrderController payOrder processPayment"
Стало: "OrderController payOrder processPayment /api/v1/orders/pay"
```

BM25 получит точный лексический сигнал для URL-ориентированных шагов.  
Gateway конфиг не трогать — достаточно аннотаций Java-кода.

**Ожидаемый эффект:** улучшение entry point accuracy для шагов типа "Pay for the order" / "Book ticket".

---

## P2. Layer-метки для фильтрации ts-common шума

**Файлы:** `scripts/gen_layer_marks.py` (новый), `src/trace/closure.py`

**Подход: разметка на уровне директории через Qwen (few-shot)**

Вместо анализа каждого метода — итерироваться по уникальным пакетам модуля, брать 3–5 примеров имён классов/методов из директории и спрашивать Qwen один раз на пакет:

```
Package: ts-common/.../entity/  
Examples: Order, OrderItem, Food, Seat, User
→ Qwen: "entity" (utils layer)

Package: ts-common/.../util/  
Examples: JsonUtils, RsaUtils, SystemTime  
→ Qwen: "utils" (utils layer)

Package: ts-common/.../security/  
Examples: JwtUtil, SecurityConfig, UserDetailsImpl  
→ Qwen: "infra" (infra layer)
```

Результат сохраняется в `cache/layer_marks.json`: `{"ts-common/.../entity": "entity", ...}`

В BFS (`closure.py`): при переходе по `calls` ребру проверить layer целевого узла по его package prefix:
- `entity` / `utils` → не добавлять в финальный результат
- `infra` → не добавлять (если не entry point)
- `domain` → добавлять как обычно

**Стоимость:** один Qwen-вызов на пакет (~50–100 пакетов в ts-common). Кешируется навсегда.

**Ожидаемый эффект:** убрать основной источник FP (OrderStatus#getCode, entity конструкторы, JsonUtils).

---

## P3. Oracle outputs подключить

**Файл:** `src/localization/searcher.py`, `src/trace/oracle.py`

После построения `hs` применить Oracle-фильтр:

```python
if oracle is not None:
    hs = {h for h in hs if oracle.can_execute_handler(h, ix)}
```

`can_execute_handler` проверяет: если handler принадлежит pay-service и `oracle.can_pay() == False` → убрать.

**Важно:** фильтровать только явные противоречия (cancel при INIT, pay при CANCELLED). Не фильтровать по умолчанию — лучше FP чем FN. `refund` при PAID — оставить (это нормально).

**Честная оценка эффекта:** скромный. Best 0.768 достигнут без Oracle. Польза — в сценариях с явной последовательностью (book→pay→cancel→rebook), где pipeline сейчас предлагает cancel-методы до оплаты.

---

## P4. Fallback при пустом ranked

**Файл:** `src/localization/searcher.py`

```python
if not ranked:
    # Pure BGE-M3 без BM25 prior
    ranked = _RERANKER.search_all(act_extended, ix, top_k=10)
if not ranked:
    # Логировать — критическая потеря
    print(f"[warn] No candidates for step: {act[:60]}", file=sys.stderr)
    continue
```

**Ожидаемый эффект:** спасает Recall для экзотических шагов с нестандартной лексикой.

---

## Последовательность реализации

| Шаг | Сложность | Риск регрессии | Эффект |
|-----|-----------|---------------|--------|
| P1 URL-токены | Низкая | Низкий | Entry point accuracy |
| P4 Fallback | Низкая | Низкий | Recall edge cases |
| P3 Oracle | Средняя | Средний | FP при статусных противоречиях |
| P2 Layer-метки | Высокая | Высокий | Precision (ts-common шум) |

Начинать с P1 + P4 — быстрые изменения, проверяемые одним pack+submit.  
P2 трогать последним — изменение closure.py ломает многое если неаккуратно.
