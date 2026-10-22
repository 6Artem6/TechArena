# Self-Discovering Dictionary Pipeline

Цель: заменить ручные словари в `domain_config.yaml` самоорганизующимся конвейером.  
Входные данные: только код + технический стоп-шум (без seed-категорий).

---

## Архитектура v2 — Bottom-Up Clustering (актуальная)

```
[ Сырые токены: module names / swagger tags / passport synonyms ]
          │
          ▼
1. BGE-M3 Batch Vectorization
   (каждый токен → embedding, L2-нормализация)
          │
          ▼
2. AgglomerativeClustering (cosine, distance_threshold=0.40)
   (нет заданного числа кластеров и нет seed-категорий)
          │
          ▼
3. Qwen Cluster Labeling
   «Вот группа токенов [consign, baggage, luggage]. Какой доменный смысл?»
   → category_id + description + positive_patterns
          │
          ▼
4. Автозапись в cache/auto_categories.json → опционально в domain_config.yaml
```

**Скрипт**: `scripts/gen_category_clusters.py`

### Почему Bottom-Up лучше seed-подхода

| | Seed-подход (v1) | Bottom-Up (v2) |
|---|---|---|
| Входные данные | seed_categories + стоп-шум | только стоп-шум |
| Seed_categories | новый вид хардкода (5 категорий) | не нужны |
| Новый домен | нужно задать категории вручную | пайплайн открывает их сам |
| Стабильность | детерминировано | зависит от distance_threshold |

---

## Архитектура v1 — Top-Down с Seed (историческая)

```
[ Код / Пути / AST / Service Passports ]
          │
          ▼
1. BM25 / Frequency Token Mining → 2. Bi-Encoder (к seed-якорям) →
3. Cross-Encoder Filtering → 4. Qwen Induction Loop → 5. yaml
```

Минимальный вход: `seed_categories` (5 категорий) + стоп-шум.

---

## Минимальный технический стоп-шум (общий для обоих подходов)

```yaml
# Инфраструктура сборки — не домен
tag_stop_min: ["service", "type", "data", "info", "result", "request", "response"]
excluded_dirs: ["target/", ".git/", "node_modules/"]
```

---

## Что пайплайн заменяет

| Текущий ключ в yaml | Статус | Заменяется на |
|---------------------|--------|---------------|
| `infra_module_tokens` | ручной | freq mining → `infrastructure` seed cluster → Qwen confirm |
| `tag_stop_base` | ручной | TF-IDF по тегам: слова с высокой частотой у всех сервисов → stop |
| `auth_token_whitelist` | ручной (✓ частично через gen_project_config) | `authentication` seed cluster |
| `auth_namespace_noise` | ручной (✓ частично через `_derive_method_noise`) | `authentication` cluster negative |
| `arch_suffix_stop` | ручной | freq analysis class name suffixes + arch_role seed |
| `arch_token_extra` | ручной | arch_role seed cluster |
| `tier2_arch_tokens` | **уже генерируется** через gen_project_config.py + Qwen | — |
| `tier4_remote_tokens` | **уже генерируется** через gen_project_config.py + Qwen | — |
| `optional_service_groups` | **уже генерируется** через tags + Qwen | — |

### Что НЕ заменяется (принципиально)

| Ключ | Причина |
|------|---------|
| `token_injection_rules` step_type → tokens | **Доказано экспериментом**: bridges из модульных токенов тянут весь BFS-конус → регрессия 0.818→0.661. Эти маппинги — не словари, а точные disambiguation hints |
| `oracle_train_patterns` | ЖД-домен: G/D/NOT_G — семантика не выводится из кода |
| `classification_rules` train_type | То же: D-train→G disambiguation — намеренный трюк |
| `security_filter` | Spring Security — framework-специфика |
| `complex_token_rules` | Pay flow семантика — не выводится из структуры кода |
| `seed_categories` | Исходный минимум — требует человеческого ввода |

---

## Что уже реализовано (этапы 1-4 частично)

В `gen_project_config.py`:
- **Этап 1**: `_extract_arch_suffix_candidates()` — freq mining суффиксов
- **Этап 1**: `_extract_auth_namespace_candidates()` — freq mining auth tokens
- **Этап 2**: tag clustering через TF-IDF (для optional_service_groups)
- **Этап 4**: Qwen step 1 — выбор optional addon кластеров
- **Этап 4**: Qwen step 2 — классификация singletons

Что добавить для полного пайплайна:
- **Шаг A**: `infra_module_tokens` generation: `first_tokens(all_modules) \ optional_groups \ core_business → Qwen confirm → infrastructure seed`
- **Шаг B**: `tag_stop_base` via TF-IDF: теги из project_profile.json → слова с IDF < threshold → stop
- **Шаг C**: Унифицированный `seed_categories` driver — один промпт вместо нескольких ad-hoc Qwen вызовов

---

## Реализация: приоритетный порядок

### P1 (быстро, без Qwen) — `tag_stop_base` через TF-IDF

Файл: `scripts/gen_project_config.py` → `_derive_tag_stop_base()`  
Данные: теги из `cache/project_profile.json`  
Алгоритм: слова встречающиеся в > 50% сервисов по тегам → stop  
Результат: убрать `tag_stop_base` из yaml → генерируется в project_config.json

```python
def _derive_tag_stop_base(service_tags: dict[str, list[str]]) -> list[str]:
    from collections import Counter
    word_counts = Counter()
    for tags in service_tags.values():
        words = set(w for t in tags for w in t.lower().split())
        word_counts.update(words)
    threshold = len(service_tags) * 0.5
    return [w for w, c in word_counts.items() if c >= threshold and len(w) > 2]
```

### P2 (средне, с Qwen) — `infra_module_tokens` generation

Файл: `scripts/gen_project_config.py` → расширить `derive_optional_groups_from_tags()`  
Алгоритм:
1. `all_first_toks = {m.split('-')[1] for m in all_modules}`
2. `optional_toks = {tok for group in optional_service_groups.values() for s in group for tok in [s.split('-')[1]]}`
3. `candidates = all_first_toks \ optional_toks`
4. Qwen confirm: "Are these infrastructure (cross-cutting, not domain) module tokens?" → filter

### P3 (архитектурный) — Унифицированный seed_categories driver

Заменяет все ad-hoc Qwen вызовы одним structured промптом:

```python
def run_seed_categorization(tokens: list[str], seed_categories: list[dict]) -> dict[str, list[str]]:
    """Один Qwen вызов → {category_id: [confirmed_tokens]}"""
```

---

## Риски

| Риск | Митигация |
|------|-----------|
| Нестабильность кластеризации между запусками | Кешировать результат в cache/; пересчитывать только при изменении скелета |
| seed_categories — новый вид хардкода | Принципиально меньше: 5 абстрактных категорий vs 50+ конкретных токенов |
| BGE-M3 при каждой регенерации | Только offline скрипты, не в prediction path |
| token_injection_rules автоматизация | **НЕ делать** — доказана регрессия |

---

## Итог

Из 8 ручных словарей в yaml:
- **3 уже генерируются** (tier2, tier4, optional_service_groups)
- **2 можно добавить** через P1/P2 (tag_stop_base, infra_module_tokens)  
- **3 остаются ручными** по принципиальным причинам (oracle_train_patterns, security_filter, token_injection_rules)

После P1+P2: yaml сократится с ~100 строк словарей до ~20 строк seed_categories + технический шум.
