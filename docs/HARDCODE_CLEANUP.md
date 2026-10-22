# Hardcode Cleanup — Переход к Data-Driven подходу

Цель: убрать всю domain-специфику trainticket из Python-кода.  
Критерий жюри: переносимость на другие репозитории + покрытие скрытых сценариев.  
Стратегия: **RuleEngine** читает declarative rules из yaml/json, заполняет `StepInfo`.  
Python-код не знает про `train_type`, `has_food`, `seat_class` — только yaml знает.

---

## Архитектура RuleEngine (целевая)

```python
# Каждое правило в domain_config.yaml:
# - target_field / target_value — что записать в StepInfo
# - priority + group — mutex (NOT_G приоритетнее G)
# - positive_patterns / negative_patterns — regex match

class RuleEngine:
    def evaluate_into(self, text: str, target: StepInfo) -> None:
        for rule in self.rules:
            if group in processed_groups: continue
            if any neg matches: continue
            if any pos matches:
                setattr(target, rule["target_field"], rule["target_value"])
```

Заполняет обычный `StepInfo` через `setattr` — type safety сохраняется, поля в Python не упоминаются.

---

## Статус по константам

### ✅ Сделано

| Константа | Файл | Итог |
|-----------|------|------|
| `_G_TRAIN` / `_OTHER_TRAIN` / `_NOT_G_TRAIN` | `step_typer.py` → `domain_config.yaml:step_train_patterns` | перенесено |
| `_HAS_FOOD` / `_NO_FOOD` / `_HAS_INS` / `_NO_INS` / `_HAS_LUG` / `_NO_LUG` | `oracle.py` → `cache/optional_patterns.json` | генерируется Qwen |
| `_ARCH_TOKEN_WHITELIST` | `gen_project_config.py` | авто через `_extract_arch_suffix_candidates()` |
| `_AUTH_TOKEN_WHITELIST` | `gen_project_config.py` | авто через `_extract_auth_namespace_candidates()` |
| `_noise` (auth namespace noise) | `gen_project_config.py` | `_derive_method_noise()` + `auth_namespace_noise` в yaml |
| `_TAG_STOP_BASE` | `gen_project_config.py` | `_load_tag_stop_base()` читает из `domain_config.yaml:tag_stop_base` |
| `_CORE_INFRA_TOKENS` | `gen_project_config.py` | `_load_infra_module_tokens()` читает из `domain_config.yaml:infra_module_tokens` |
| `arch_suffix_stop` / `arch_token_extra` | `gen_project_config.py` | в `domain_config.yaml` |
| `auth_token_whitelist` / `auth_namespace_noise` | `gen_project_config.py` | в `domain_config.yaml` |
| `_SAFE_KEYWORD_TYPES` | `step_typer.py` | в `domain_config.yaml:safe_keyword_step_types` |
| `_SEAT_FIRST` / `_SEAT_SECOND` | `step_typer.py` | `domain_config.yaml:classification_rules` + `RuleEngine` |
| `_PAY_INTERNAL` / `_PAY_EXTERNAL` / `_PAY_EMPTY` | `step_typer.py` | `domain_config.yaml:classification_rules` + `RuleEngine` |
| `_SYSTEM_REFUSES` | `step_typer.py` | `domain_config.yaml:classification_rules` + `RuleEngine` |

### 🔴 Не сделано — Группа A: перенести в yaml (быстро)

Эти константы — лингвистические паттерны, не зависят от скелета.  
После переноса `step_typer.py` не содержит ни одного hardcoded regex.

| Константа | Где сейчас | Целевое место | Приоритет |
|-----------|-----------|--------------|-----------|
| `_PAY_INTERNAL` / `_PAY_EXTERNAL` / `_PAY_EMPTY` | `step_typer.py` hardcode | `domain_config.yaml:step_pay_patterns` + RuleEngine | P1 |
| `_SYSTEM_REFUSES` | `oracle.py` hardcode | `domain_config.yaml:system_refuses_pattern` + RuleEngine | P1 |
| `_SEAT_FIRST` / `_SEAT_SECOND` | `step_typer.py` hardcode | `domain_config.yaml:step_seat_patterns` + RuleEngine | P1 |

Yaml-схема:

```yaml
step_pay_patterns:
  - rule_id: pay_empty
    target_field: pay_type
    target_value: empty
    priority: 30
    group: pay_type
    positive_patterns: ['\\binternal\\b.{0,50}\\bempty\\b']
    negative_patterns: []
  - rule_id: pay_internal
    target_field: pay_type
    target_value: internal
    priority: 20
    group: pay_type
    positive_patterns: ['\\binternal\\b']
    negative_patterns: []
  - rule_id: pay_external
    target_field: pay_type
    target_value: external
    priority: 10
    group: pay_type
    positive_patterns: ['\\bexternal\\b']
    negative_patterns: []

step_seat_patterns:
  - rule_id: seat_first
    target_field: seat_class
    target_value: FIRST
    priority: 10
    group: seat_class
    positive_patterns: ['\\b(first.?class|business.?class|1st.?class)\\b']
    negative_patterns: []
  - rule_id: seat_second
    target_field: seat_class
    target_value: SECOND
    priority: 10
    group: seat_class
    positive_patterns: ['\\b(second.?class|economy|2nd.?class|standard)\\b']
    negative_patterns: []

system_refuses_rules:
  - rule_id: system_refuses
    target_field: is_refused
    target_value: true
    priority: 10
    group: null
    positive_patterns: ['\\bthe\\s+system\\s+(refuses|rejects|denies)\\b']
    negative_patterns: []
```

### 🟡 Не сделано — Группа B: derive из скелета (средне)

| Константа | Метод | Приоритет |
|-----------|-------|-----------|
| `_OPTIONAL_SEED_KEYWORDS` (`food/meal/insurance/consign/...`) | sibling pairs из `sibling_metadata.json` → общий токен в имени → seed. Нет hardcoded категорий. | P2 |
| `_build_extra_tokens()` step_type→token bridges | offline BM25 co-occurrence: `scripts/gen_step_token_bridges.py` → `cache/step_token_bridges.pkl` | P3 |

**`_OPTIONAL_SEED_KEYWORDS` — высокий impact:**  
Сейчас: `frozenset({"food", "meal", "dining", "consign", "consignment", "luggage", "baggage", "insurance", "assurance", "protection"})` — trainticket specific.  
После: `_derive_optional_seeds(sibling_metadata)` — для любого проекта найдёт токены sibling пар. Нет упоминаний конкретных domain слов.

### 🟠 Не сделано — Группа C: архитектурный рефакторинг (сложнее)

| Компонент | Проблема | Подход |
|-----------|---------|--------|
| `_is_g_service()` в `closure.py` | regex `ts-[a-z]+-g-service` — trainticket service naming | заменить на `_is_secondary_variant(mod, sibling_metadata)` — generic |
| sibling force-swap в `searcher.py` | знает про G/OTHER концепцию | generic "swap to sibling variant" по `sibling_metadata` |
| `train_type` в `StepRecord` | trainticket concept в ядре | `variant_type` + читать field name из yaml |
| Oracle FSM states | PAID/CANCELLED/INIT — domain specific | configurable states из yaml (менее критично) |

---

## RuleEngine — план реализации

### ✅ Шаг 1 (P1): `src/rule_engine.py` создан

```python
class RuleEngine:
    def evaluate_into(self, text: str, target: Any) -> None:
        # pure loop: no field names, no domain knowledge
        # setattr(target, rule["field"], rule["value"])
```

### ✅ Шаг 2 (P1): 6 констант удалены из `step_typer.py`, правила в yaml

Файлы изменены: `step_typer.py` (c8e9f87), `domain_config.yaml`  
`StepInfo.pay_type: str = ""` добавлен как новое поле.

### ✅ Шаг 2b: `scripts/gen_classification_rules.py` — генерация правил из скелета через Qwen

Данные для промпта: `_extract_enum_summary(sk)` + `_extract_service_roles(sk)`  
Qwen видит: `SeatClass: FIRSTCLASS, SECONDCLASS, BUSINESS...` → генерирует NL regex  
Post-processing: нормализация `target_value`, fix regex alternation без группы  
Сохраняет в `cache/generated_classification_rules.json` для ревью перед merge в yaml

### Шаг 3 (P2): `_derive_optional_seeds()` из sibling_metadata

Файл: `gen_project_config.py`  
Заменить `_OPTIONAL_SEED_KEYWORDS` frozenset на функцию которая читает `cache/sibling_metadata.json`.

### Шаг 4 (P3): `gen_step_token_bridges.py` + убрать bridges из `_build_extra_tokens()`

Скрипт генерирует `cache/step_token_bridges.pkl` offline.  
`_build_extra_tokens()` читает из кеша вместо hardcoded if-blocks.

---

## Что НЕ трогаем

- BM25 + BGE-M3 + CrossEncoder — domain-agnostic по определению ✅
- `optional_service_groups` генерация — уже portable ✅
- `tier2/tier4` токены — генерируются из скелета ✅
- `excluded_modules` логика — читает из `optional_service_groups` ✅
- FSM transitions (can_pay/can_cancel) — domain-specific но не hardcode в плохом смысле, это правильная бизнес-логика
