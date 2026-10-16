# ADR-018: Unified Rule Store, ripgrep pre-filter, rule-triggered reindex

**Статус:** Принято (Local Mode: YAML + in-memory; PostgreSQL-хранение — Deferred)  
**Дата:** 2026-10-13  
**Связанные:** ADR-001, ADR-004, ADR-011, ADR-014, ADR-022

## Контекст

Правила поиска и классификации живут в шести разных местах (см. анализ ниже).
Это создаёт три конкретные проблемы:

1. **Дублирование**: ROLE_MAP (ADR-004), SHELL_PATTERNS (ADR-011) и framework profiles (ADR-017) описывают одну концепцию разными инструментами.
2. **Мёртвые правила**: новое Ollama-правило сохраняется в `generated_rules`, но уже проиндексированные файлы не пересматриваются.
3. **Медленный первый проход**: tree-sitter парсит каждый файл полностью, хотя большинство файлов не содержат нужных паттернов.

---

## Анализ: все правила — одна структура

| Текущее место | Паттерн | Действие |
|---|---|---|
| `env_patterns.yaml` | value_prefix: "jdbc:" | label: SHARES_RESOURCE |
| `shadow_patterns.yaml` | text: "restTemplate.exchange" | label: code |
| `framework_profiles/java_spring_boot.yaml` | annotation: "@RequestMapping" | role: http_handler |
| `ROLE_MAP` dict (ADR-004) | dir_name: "controller" | role: http_handler |
| `generated_rules` table | tree_sitter_query: "(annotation...)" | label: CALLS_SERVICE |
| `SHELL_PATTERNS` (ADR-011) | regex: `curl\s+.*?(https?://...)` | label: CALLS_SERVICE |

Все шесть — это `(trigger, pattern_type, pattern) → (label, edge_type, confidence)`.

---

## Решение A: Единая схема правил — YAML + in-memory (Local Mode)

### Единая структура

Все правила — одна структура `Rule`, источники: YAML-файлы + Framework Profiles (ADR-022):

```python
@dataclass
class Rule:
    id: str                  # уникальный slug, напр. "spring_rest_template"
    source: str              # seed | framework_profile | generated | builtin
    language: str | None     # java | python | go | None (универсальное)
    framework: str | None    # spring_boot | flask | None
    scope: str               # env_var | code_annotation | code_call |
                             # package_name | comment | shell | config
    pattern_type: str        # regex | tree_sitter_query | string_match | glob
    pattern: str
    label: str               # CALLS_SERVICE | SHARES_RESOURCE | code | http_handler | ...
    edge_type: str | None    # CALLS_SERVICE | SHARES_RESOURCE | ASYNC_MESSAGE | None
    confidence: str          # HIGH | MEDIUM | LOW
    example_text: str | None = None  # seed-пример для BGE-M3 fallback
    status: str = "active"   # active | deprecated | pending_validation
```

### YAML-формат правил (sources/env_patterns.yaml)

```yaml
rules:
  - id: jdbc_datasource_url
    source: seed
    scope: env_var
    pattern_type: string_match
    pattern: "jdbc:"
    label: SHARES_RESOURCE
    edge_type: SHARES_RESOURCE
    confidence: HIGH
    example_text: "SPRING_DATASOURCE_URL=jdbc:mysql://ts-order-mysql:3306/ts-order"

  - id: http_service_url
    source: seed
    scope: env_var
    pattern_type: regex
    pattern: "https?://[a-z0-9-]+"
    label: CALLS_SERVICE
    edge_type: CALLS_SERVICE
    confidence: HIGH
    example_text: "ORDER_SERVICE_URL=http://ts-order-service:12031"
```

### Загрузка правил при старте (Local Mode)

```python
def load_rules(rules_dir: Path) -> list[Rule]:
    """Загружает все YAML-правила из директории в память."""
    rules = []
    for yaml_file in sorted(rules_dir.glob("*.yaml")):
        data = yaml.safe_load(yaml_file.read_text())
        for r in data.get("rules", []):
            rules.append(Rule(**r))
    return [r for r in rules if r.status == "active"]

# Единая точка входа — RuleEngine работает с in-memory list
class RuleEngine:
    def __init__(self, rules_dir: Path):
        rules = load_rules(rules_dir)
        self._by_scope: dict[str, list[Rule]] = {}
        for r in rules:
            self._by_scope.setdefault(r.scope, []).append(r)
```

> **Production (Deferred):** PostgreSQL `rules` таблица с pgvector-индексом для fuzzy-поиска по embedding. Схема SQL, миграции и rule-triggered reindex через `rules_version` счётчик реализуются после хакатона (ADR-008).

### Применение правил при парсинге

```python
class RuleEngine:
    """Централизованное применение правил."""

    def __init__(self, rules: list[Rule]):
        # Правила кешируются в памяти при старте воркера
        self._rules_by_scope: dict[str, list[Rule]] = group_by(rules, key="scope")

    def match(self, scope: str, text: str, language: str = None) -> RuleMatch | None:
        """Найти первое совпадающее правило.
        
        Порядок: HIGH confidence → MEDIUM → fuzzy по cosine similarity.
        """
        candidates = [r for r in self._rules_by_scope.get(scope, [])
                      if r.language in (language, None)]

        # 1. Точное совпадение (HIGH confidence первым)
        for rule in sorted(candidates, key=lambda r: r.confidence_order):
            if self._matches(rule, text):
                return RuleMatch(rule=rule, match_type="exact")

        # 2. Fuzzy: BGE-M3 embedding поиска по cosine similarity
        query_vec = bge_m3.embed(text)
        best = find_nearest_rule(query_vec, candidates, threshold=0.80)
        if best:
            return RuleMatch(rule=best.rule, match_type="fuzzy",
                             confidence_override="MEDIUM")

        return None
```

Fuzzy matching покрывает случай "директория называется `impl` вместо `service`" — косинусное расстояние к известным ролям > 0.8.

---

## Решение B: ripgrep как первый проход перед tree-sitter

### Проблема

tree-sitter парсит каждый файл полностью (~5–50ms/файл × 635 Java файлов = секунды). Большинство файлов не содержат нужных паттернов — полный парсинг избыточен.

### Двухпроходная схема

```
Шаг 1: ripgrep — быстро (< 1 секунды на весь репозиторий)
  - Берём все regex-паттерны из rules (scope=code_call, scope=env_var, ...)
  - ripgrep возвращает: {file_path: [line_number, pattern_id]}
  - Файлы без совпадений → только минимальный парсинг (имя класса, сервис)

Шаг 2: tree-sitter — только для файлов с совпадениями
  - Полный AST-парсинг + tree_sitter_query правила
  - На 20–30% файлов вместо 100%
```

```python
def prefilter_files(repo_path: Path, rules: list[Rule]) -> dict[Path, list[str]]:
    """Возвращает {файл: [сработавшие pattern_id]}.
    
    ripgrep CLI: быстрее Python regex в 5–10 раз, работает параллельно.
    """
    # Собираем все regex паттерны из rules
    regex_patterns = [r.pattern for r in rules if r.pattern_type == "regex"]
    
    # Один вызов ripgrep со всеми паттернами через --regexp
    # Флаги: --json для структурированного вывода, --multiline для блоков
    result = subprocess.run([
        "rg", "--json", "--multiline",
        *[f"--regexp={p}" for p in regex_patterns],
        str(repo_path)
    ], capture_output=True)
    
    return parse_rg_json(result.stdout)  # {path: [matched_patterns]}
```

### Aho-Corasick для точных строк (дополняет ripgrep)

ripgrep эффективен для regex-паттернов. Для точных строк (имена аннотаций `@RequestMapping`, имена классов `RestTemplate`) — Aho-Corasick строит один автомат из всех паттернов и делает **один проход** по файлу:

```python
import ahocorasick

def build_exact_pattern_automaton(rules: list[Rule]) -> ahocorasick.Automaton:
    """O(n + m + k): n = размер файла, m = суммарная длина паттернов, k = совпадения."""
    A = ahocorasick.Automaton()
    for rule in rules:
        if rule.pattern_type == "string_match":
            A.add_word(rule.pattern, rule.id)
    A.make_automaton()
    return A
```

Разделение: **ripgrep** для regex (структурные паттерны, URL-регулярки), **Aho-Corasick** для точных строк (аннотации, имена методов). Оба дают совпадения за O(n).

### Что даёт

| Тип файла | Без pre-filter | С pre-filter |
|---|---|---|
| `InitData.java` (только seed data) | full parse | пропускаем (нет HTTP паттернов) |
| `OrderServiceImpl.java` | full parse | full parse (содержит `restTemplate`) |
| Lombok entity | full parse | минимальный parse (нет вызовов) |
| Ресурсные файлы `.properties` | full parse | ripgrep находит URL-переменные |

Ожидаемое ускорение: ~3x для Java, ~5x для конфиг-файлов.

---

## Решение C: Rule-triggered reindex

### Local Mode — хэш правил

В Local Mode граф строится заново при каждом запуске (или восстанавливается из pickle). При добавлении новых правил в YAML — следующий запуск автоматически получает обновлённый набор. Явного версионирования не нужно.

```python
def rules_fingerprint(rules_dir: Path) -> str:
    """MD5 от всех YAML-файлов правил — меняется при добавлении/изменении правила."""
    h = hashlib.md5()
    for f in sorted(rules_dir.glob("*.yaml")):
        h.update(f.read_bytes())
    return h.hexdigest()

def should_rebuild_graph(cache_meta: dict, rules_dir: Path, repo_hash: str) -> bool:
    """Граф невалиден если изменился репо или правила."""
    return (
        cache_meta.get("repo_hash") != repo_hash or
        cache_meta.get("rules_hash") != rules_fingerprint(rules_dir)
    )
```

> **Production (Deferred):** Rule-triggered selective reindex через PostgreSQL `rules_version` счётчик + Redis. Только изменённые файлы переиндексируются. Реализовать после хакатона.

---

## Связи с другими ADR (что меняется)

### ADR-004: ROLE_MAP → переносится в YAML-правила

```yaml
# Было: Python dict (хардкод)
ROLE_MAP = {"controller": "http_handler", "service": "business_logic", ...}

# Становится: записи в rules/builtin_roles.yaml
# scope=package_name, pattern_type=string_match, label=http_handler, source=builtin
```

`ROLE_MAP` Python-dict остаётся только для bootstrap при первом запуске — генерирует `builtin_roles.yaml`, потом убирается из runtime.

### ADR-011: SHELL_PATTERNS и PYTHON_HTTP_CALL_PATTERNS → YAML-правила

```yaml
# scope=shell, language=null, pattern_type=regex → rules/shell_patterns.yaml
# scope=code_call, language=python, pattern_type=regex → rules/python_patterns.yaml
```

### ADR-014: few-shot примеры → `example_text` поле в Rule

Few-shot примеры ENV_PATTERN хранятся как Rule с заполненным `example_text`. BGE-M3 кодирует их при старте RuleEngine.

### ADR-022: Framework Profiles → отдельный YAML-источник

Framework profiles (ADR-022 §F) загружаются как отдельный источник `source=framework_profile` при старте RuleEngine — не хардкод, не PostgreSQL.

---

## Последствия

- Local Mode: `load_rules(rules_dir)` при каждом старте, in-memory list — без PostgreSQL, без Redis.
- Python-хардкод ROLE_MAP и SHELL_PATTERNS → мигрировать в YAML при реализации.
- ripgrep — дополнительная зависимость для pre-filter (`brew install ripgrep`). Опциональна: без ripgrep pre-filter отключается, tree-sitter работает по всем файлам.
- Production (Deferred): PostgreSQL `rules` таблица + pgvector, rule_versions, Redis-версионирование — реализовать после хакатона.
