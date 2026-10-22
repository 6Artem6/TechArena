# Plan: Hardcode Cleanup — Автогенерация констант

Цель: все domain-специфичные константы выводить из скелета проекта, а не кодировать вручную.  
Стратегия: keyword search в методах/путях/DTO скелета, статистика по модулям, перенос в config.

---

## Статус по каждой константе

### Уже автогенерируется (не трогать)

| Константа | Файл | Метод генерации |
|-----------|------|----------------|
| `_TAG_STOP_BASE` | `gen_project_config.py` | `derive_high_freq_tokens()` → top-frequency токены тегов; базис остаётся как fallback |
| `_ARCH_TOKEN_WHITELIST` | `gen_project_config.py` | `_extract_arch_suffix_candidates()` → CamelCase суффиксы классов в ≥20% модулей |
| `_AUTH_TOKEN_WHITELIST` | `gen_project_config.py` | `_extract_auth_namespace_candidates()` → токены методов auth/security/* модулей |
| `_HAS_FOOD/NO_FOOD/etc` | `oracle.py`, `step_typer.py` | `build_optional_re()` читает из `cache/optional_patterns.json` → генерируется `gen_project_config.py` |

---

### Группа A: Перенести в domain_config.yaml (нет зависимости от скелета)

Эти паттерны — лингвистические, не зависят от конкретного проекта.  
Сейчас хардкод в `step_typer.py`. Достаточно переноса в `src/domain_config.yaml`.

| Константа | Сейчас | Целевое место | Метод |
|-----------|--------|--------------|-------|
| `_G_TRAIN` | `_build_train_re("G")` | **уже** в `domain_config.yaml:step_train_patterns.G` | ✅ готово |
| `_OTHER_TRAIN` | `_build_train_re("OTHER")` | **уже** в `domain_config.yaml` | ✅ готово |
| `_NOT_G_TRAIN` | `_build_train_re("NOT_G")` | **уже** в `domain_config.yaml` | ✅ готово |
| `_PAY_INTERNAL` | `re.compile(r"\binternal\b")` | `domain_config.yaml:step_pay_patterns.internal` | перенести |
| `_PAY_EXTERNAL` | `re.compile(r"\bexternal\b")` | `domain_config.yaml:step_pay_patterns.external` | перенести |
| `_PAY_EMPTY` | `re.compile(r"\binternal\b.{0,50}\bempty\b")` | `domain_config.yaml:step_pay_patterns.empty` | перенести |
| `_SYSTEM_REFUSES` | `re.compile(r'\bthe\s+system\s+(refuses\|rejects\|denies)\b')` | `domain_config.yaml:system_refuses_pattern` | перенести |

**Реализация**: добавить блок в `domain_config.yaml`, читать через `cfg_get()` в `step_typer.py`.

---

### Группа B: Derive из скелета (следующие приоритеты)

#### `_noise` (внутри `_extract_auth_namespace_candidates`)
- **Сейчас**: frozenset с `cors, configure, mapping, get, add, find, create, delete...`
- **Метод**: `derive_high_freq_tokens(svc_tokens, n_modules, threshold=0.60)` уже существует.  
  Токены встречающиеся у ≥60% всех модулей — это universal noise, не специфика auth.
- **Реализация**: вызвать внутри `_extract_auth_namespace_candidates` вместо хардкода.

#### `_CORE_INFRA_TOKENS`
- **Сейчас**: `auth, security, gateway, common, wait, inside, admin, execute, delivery, notification, verification`
- **Метод**: Инфра-сервисы = модули без бизнес-endpoints (нет REST @GetMapping/@PostMapping с domain-путями).  
  В скелете: сервисы у которых ВСЕ endpoints — admin/config/health/actuator путей.  
  Первые токены их имён → infra tokens.
- **Реализация**: `_extract_infra_module_tokens(sk)` → модули без `/api/v*/` endpoints → split("-") → first tokens.
- **Сложность**: средняя.

#### `_OPTIONAL_SEED_KEYWORDS`
- **Сейчас**: food/meal/dining/catering, insurance/assurance/protection, consign/consignment/luggage/baggage
- **Bootstrap problem**: seed нужны для нахождения optional groups, а groups нужны для seeds.
- **Метод**: Singletons с count=2 (sibling pairs: food-service/station-food-service → "food") → name tokens  
  → intersect с `_has_optional_keyword_in_skeleton()`. Это self-consistent: имена сервисов уже содержат тип.
- **Реализация**: `_derive_optional_seeds(modules, sibling_pairs)`:
  1. Найти сервисы у которых есть sibling с "-station-"/"-train-" prefix → domain category
  2. Split sibling module names → keyword tokens
  3. Пример: `ts-food-service` + `ts-station-food-service` → `{"food"}`, `ts-train-food-service` → `{"food"}`
- **Сложность**: низкая — имена модулей уже содержат category token.

#### `_SEAT_FIRST` / `_SEAT_SECOND` 
- **Сейчас**: `re.compile(r"\b(first.?class|business.?class|1st.?class)\b")` — хардкод
- **Метод**: В скелете seat entity обычно содержит enum или constants с FIRST/SECOND/BUSINESS/ECONOMY.  
  `grep kinds=["field"] name ~ /FIRST|SECOND|ECONOMY|BUSINESS/` → CamelCase tokens → regex  
  Или: перенести в `domain_config.yaml:step_seat_patterns` (проще).
- **Реализация**: `_extract_seat_class_tokens(sk)` ищет enum поля в seat/* entity модулях.

#### `_SAFE_KEYWORD_TYPES`  
- **Сейчас**: `frozenset(cfg_get("safe_keyword_step_types", [...]))` — уже в `domain_config.yaml`
- **Статус**: ✅ уже выведено в конфиг, просто не видно что это так.

---

### Группа C: Нецелесообразно автогенерировать

| Константа | Причина |
|-----------|---------|
| `_SYSTEM_REFUSES` | Лингвистический паттерн "the system refuses". Нет корреляции со скелетом. Перенести в `domain_config.yaml`. |
| `_TAG_STOP_BASE` (базис) | Generic English stop-words. Не зависят от проекта — оставить как есть. |
| `_BASE_PATTERN_KEYS` | Не существует как отдельная константа — keys derive из optional group names автоматически. |

---

## Приоритетный порядок реализации

```
P1  _noise → derive_high_freq_tokens(threshold=0.60)                    [1ч, легко]
P2  _PAY_INTERNAL/EXTERNAL/EMPTY → domain_config.yaml                   [30мин]
P3  _SYSTEM_REFUSES → domain_config.yaml                                [15мин]
P4  _OPTIONAL_SEED_KEYWORDS → _derive_optional_seeds(modules, siblings) [2ч]
P5  _CORE_INFRA_TOKENS → _extract_infra_module_tokens(sk)               [2ч]
P6  _SEAT_FIRST/_SECOND → domain_config.yaml или skeleton enum          [1ч]
```

---

## Что НЕ трогать

- `_G_TRAIN` / `_OTHER_TRAIN` / `_NOT_G_TRAIN` — уже из `domain_config.yaml`.
- `_HAS_FOOD` / `_NO_FOOD` / ... — уже из `optional_patterns.json` (генерируется).
- `_ARCH_TOKEN_WHITELIST` базис — нужен как fallback при первом запуске без Qwen.
- `_AUTH_TOKEN_WHITELIST` базис — аналогично.

---

## Дополнение: `step_typer.py` hardcodes (строки 48-81)

Перенести в `domain_config.yaml`:
```yaml
step_seat_patterns:
  first: "first.?class|business.?class|1st.?class"
  second: "second.?class|economy|2nd.?class|standard"

step_pay_patterns:
  internal: "\\binternal\\b"
  external: "\\bexternal\\b"
  empty: "\\binternal\\b.{0,50}\\bempty\\b"

system_refuses_pattern: "\\bthe\\s+system\\s+(refuses|rejects|denies)\\b"
```

`step_typer.py` читает через `cfg_get()` аналогично `_build_train_re()`.  
После этого `step_typer.py` не содержит ни одного хардкоженного regex.
