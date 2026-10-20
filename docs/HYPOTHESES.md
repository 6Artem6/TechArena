# Гипотезы регрессии IoU 0.768 → 0.703

Лучший результат: `submission_best.zip` → **0.807 0.737 0.802 0.808 0.741 0.801 0.683 / 0.768330**  
Текущий (до хаков): 0.777 0.705 0.647 0.756 0.632 0.770 0.641 / 0.703970  
Формат: S1 S2 S3 S4 S5 S6 S7 / avg

Схема: **Теория → Проверка (граф/код) → Хак → Чистая реализация**

---

| # | Гипотеза | Статус | Δ score |
|---|---------|--------|---------|
| H1 | SP.1 passports шум в CrossEncoder | todo | ? |
| H2 | view_trip prompt fix сломал carry-over цепочку | todo | ? |
| H3 | safe keywords инжектируют неверные BM25 токены | todo | ? |
| H4 | contacts — CE/prefer переставляет findContactsByAccountId | todo | ? |
| H5 | ts-common SeatClass enum filter срезает TP | todo | ? |
| H6 | excluded_modules удаление → food FP для no-food сценариев | todo | ? |
| H7 | S5 transit FP (+22) от func_variant disambiguation | todo | ? |
| H8 | _CARRY_OVER_STEP_TYPES расходится с архивным _STEP_TYPES_USE_CONTEXT | todo | ? |
| H9 | SV gamma boost поднимает неправильные сервисы | todo | ? |
| H10 | Возврат к b5f9628 коду как baseline | todo | ? |

---

## H1: SP.1 passports добавляют шум в CrossEncoder

**Теория:** 08d0895 добавил passport content в `_CE_DOC_TEXTS`. CE — финальная стадия ранжирования.  
Если паспорта содержат misleading описания → CE переставляет правильные handler'ы ниже tau.

**Проверка:** как паспорта вливаются в CE doc_texts.  
**Хак:** `_SV_GAMMA = 0` + отключить passport в CE doc_texts.  
**Чистая:** фильтр качества паспортов перед инжекцией в CE.

---

## H2: P1.2 view_trip prompt fix изменил train_type carry-over цепочку

**Теория:** cab741b изменил промпт view_trip. Если до фикса view_trip давал `train_type=""` →  
`oracle.train_type=""` на момент book шага → `effective_train_type=""` → нет disambiguation →  
оба preserve сиблинга остаются в hs → hub chain включается.

**Проверка:** `git show cab741b -- src/localization/qwen_encoder.py | grep -A5 view_trip`  
**Хак:** убрать train_type из step_info для view_trip (вернуть "").  
**Чистая:** explicit carry-over rule в oracle FSM.

---

## H3: P1.1 safe keywords инжектируют неверные BM25 токены

**Теория:** cab741b добавил "safe keywords" — токены принудительно добавляемые в BM25 запрос.  
Могут поднимать нерелевантные handler'ы выше tau → FP.

**Проверка:** найти "safe" в step_typer.py/searcher.py.  
**Хак:** `safe_keywords = []` — убрать инжекцию.  
**Чистая:** entity-vector scoring вместо keyword injection.

---

## H4: contacts — CrossEncoder переставляет findContactsByAccountId ниже getContactsByContactsId

**Теория:** BM25 правильно ставит findContactsByAccountId (0.70) выше getContactsByContactsId (0.64).  
CE или `_prefer_step_typed_handler` меняет порядок → -2 exec/сценарий.

**Проверка:** логика выбора для step_type=get_contact.  
**Хак:** добавить get_contact в исключения prefer OR явно добавить findContactsByAccountId.  
**Чистая:** entity-vector (entity=ACCOUNT → findByAccount выигрывает).

---

## H5: ts-common SeatClass enum filter срезает TP методы

**Теория:** `_is_enum_method` фильтрует SeatClass.getNameByCode и т.д. Архив их имел.  
Если они в ground truth → +3/сценарий × 7 = +21 executes.

**Проверка:** `diff_predictions.py arch cur --scenario S2 | grep common` — сколько common методов в архиве.  
**Хак:** убрать SeatClass из enum filter ИЛИ поднять порог.  
**Чистая:** enum detection только по `kind==enum` из skeleton.

---

## H6: excluded_modules удаление → food FP для no-food сценариев

**Теория:** Убрали food exclusion, считая GT всегда включает food. Но может для "without food" сценариев  
GT не включает → +5 food методов как FP.

**Проверка:** diff arch vs cur для S6 ("without food") — есть ли food в архиве.  
**Хак:** вернуть exclusion только для `has_food is False` (не None).  
**Чистая:** food inclusion через FSM precondition вместо oracle flag.

---

## H7: S5 transit FP (+22) от func_variant disambiguation

**Теория:** S5 transit имеет +22 лишних executes (travel-service, route-plan, travel-plan).  
Func_variant disambiguation для transit шага работает неправильно.

**Проверка:** step log S5 — какие handler'ы на search_transit шаге.  
**Хак:** добавить transit в список где travel2/plan предпочтительнее basic.  
**Чистая:** func_variant inference из step action+entity frame.

---

## H8: _CARRY_OVER_STEP_TYPES расходится с архивным _STEP_TYPES_USE_CONTEXT

**Теория:** Мы добавили `_CARRY_OVER_STEP_TYPES` gate, но он может отличаться от архивного set.  
Если в него попали лишние/пропущены нужные step_types → неверная disambiguation.

**Проверка:** сравнить `_STEP_TYPES_USE_CONTEXT` в b5f9628 с текущим `_CARRY_OVER_STEP_TYPES`.  
**Хак:** синхронизировать с архивным набором.  
**Чистая:** автоматический вывод carry-over из FSM transitions.

---

## H9: SV gamma boost поднимает неправильные сервисы

**Теория:** `_SV_GAMMA = 0.12` добавляет boost handler'ам чьи паспорт-токены совпадают с запросом.  
Если паспорт содержит generic слова → boost слишком широкий → FP.

**Проверка:** `_SV_DISTINCTIVE_TOKS` — насколько distinctive реально distinctive токены.  
**Хак:** `_SV_GAMMA = 0` — отключить, смотреть score.  
**Чистая:** entity-vector scoring заменяет SV boost.

---

## H10: Возврат к b5f9628 коду как baseline

**Теория:** Все изменения после b5f9628 суммарно дают -0.065. Самый быстрый путь:  
checkout b5f9628 → применить только excluded_modules revert → submit.

**Проверка:** `git diff b5f9628 HEAD -- src/` — посмотреть объём изменений.  
**Хак:** это и есть хак — прямой откат к рабочему baseline.  
**Чистая:** cherry-pick только нужных фиксов поверх архива.
