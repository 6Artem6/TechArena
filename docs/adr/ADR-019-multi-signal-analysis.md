# ADR-019: Мульти-сигнальный анализ (Multi-Signal Analysis Framework)

**Статус:** Принято  
**Дата:** 2026-10-13  
**Связанные:** ADR-001, ADR-002, ADR-003, ADR-004, ADR-011, ADR-014, ADR-018

## Контекст

Каждый критический вопрос при построении графа (как называется сервис? какую роль играет класс? вызывает ли метод другой сервис?) имеет несколько независимых источников ответа. Ни один источник не гарантирован:

- `docker-compose.yml` может отсутствовать (некоторые репозитории идут без него)
- `spring.application.name` может не быть в `application.yml`
- `getServiceUrl()` может вести к базовому классу в другом файле
- Файл может не парситься из-за синтаксической ошибки

Текущий подход во многих местах предполагает наличие одного источника. Нужна **систематическая модель деградации**: несколько тактик на каждый вопрос, механизм слияния конфликтующих сигналов и изоляция ошибок на правильном уровне.

---

## Решение A: EnvironmentProfile — определение доступных источников

Первое действие анализатора — опрос окружения. Это определяет какие тактики активировать.

```python
@dataclass
class EnvironmentProfile:
    # Конфиг-уровень
    has_compose: bool           # docker-compose.yml найден
    has_gateway_yaml: bool      # ts-gateway-service/application.yml
    has_pom: bool               # Maven multi-module pom.xml
    has_env_files: bool         # .env, .env.* файлы
    has_helm: bool              # Helm charts
    # Код-уровень
    language_set: set[str]      # java, python, go, js
    has_symbol_index: bool      # SymbolIndex успешно построен
    service_count: int
    # Tier: богатство окружения (1=полное, 4=только директории)
    tier: int


def detect_environment(repo_path: Path) -> EnvironmentProfile:
    compose = (repo_path / "docker-compose.yml").exists() or \
              (repo_path / "docker-compose.yaml").exists()
    gateway = any(
        (repo_path / s / "src/main/resources/application.yml").exists()
        for s in repo_path.iterdir()
        if s.is_dir() and "gateway" in s.name.lower()
    )
    pom = (repo_path / "pom.xml").exists()
    env_files = bool(list(repo_path.glob(".env*")))
    langs = detect_languages(repo_path)
    svc_count = count_services(repo_path)

    # Tier: чем больше источников — тем богаче
    score = sum([compose, gateway, pom, env_files, bool(langs)])
    tier = 1 if score >= 4 else 2 if score >= 2 else 3 if score >= 1 else 4

    return EnvironmentProfile(
        has_compose=compose, has_gateway_yaml=gateway, has_pom=pom,
        has_env_files=env_files, has_helm=False, language_set=langs,
        has_symbol_index=False, service_count=svc_count, tier=tier
    )
```

**Четыре Tier:**

| Tier | Характеристика | Доступные тактики |
|---|---|---|
| 1 | docker-compose + gateway YAML + pom + .env | Все тактики |
| 2 | Только исходники + application.yml | Без compose-сигналов |
| 3 | Только исходники | Только code-based тактики |
| 4 | Только директории | Vector bootstrap |

---

## Решение B: Таблица тактик для критических вопросов

### Q1: Как называется сервис?

| Тактика | Уверенность | Tier | Источник |
|---|---|---|---|
| docker-compose service key | 0.95 | 1 | compose |
| `spring.application.name` | 0.95 | 1–2 | application.yml |
| `pom.xml → <artifactId>` | 0.80 | 1–2 | pom.xml |
| Имя директории (`ts-order-service`) | 0.75 | 1–4 | filesystem |
| BGE-M3 по первым 5 файлам + структуре | 0.50 | 4 | vector fallback |

```python
def detect_service_name(service_dir: Path, profile: EnvironmentProfile) -> Signal:
    signals = []

    if profile.has_compose:
        if name := compose_index.service_name_for(service_dir):
            signals.append(Signal(name, conf=0.95, source="docker_compose"))

    if app_yml := find_application_yml(service_dir):
        if name := parse_spring_application_name(app_yml):
            signals.append(Signal(name, conf=0.95, source="spring_app_name"))

    if profile.has_pom:
        if name := parse_pom_artifact_id(service_dir / "pom.xml"):
            signals.append(Signal(name, conf=0.80, source="pom"))

    # Всегда добавляем имя директории как низкоприоритетный fallback
    signals.append(Signal(service_dir.name, conf=0.75, source="dir_name"))

    if not signals or max(s.conf for s in signals) < 0.65:
        if profile.tier == 4:
            name = bootstrap_service_name_from_vectors(service_dir)
            signals.append(Signal(name, conf=0.50, source="vector_bootstrap"))

    return fuse_signals(signals)
```

---

### Q2: Какую роль играет класс?

| Тактика | Уверенность | Источник |
|---|---|---|
| Java-аннотация: `@RestController`, `@Service` | 0.95 | AST |
| Python-декоратор: `@app.route` | 0.95 | AST |
| Пакетный путь: `*/controller/*` | 0.80 | filesystem |
| Суффикс класса: `*Controller`, `*ServiceImpl` | 0.75 | AST |
| Суперкласс через SymbolIndex | 0.70 | SymbolIndex |
| Imports: `import *.web.bind.annotation.*` | 0.65 | AST |
| Fuzzy через BGE-M3 в rules table | 0.55 | vector |
| BGE-M3 по первому блоку файла | 0.45 | vector fallback |

---

### Q3: Вызывает ли метод другой сервис?

| Тактика | Уверенность | Примечание |
|---|---|---|
| `getServiceUrl("ts-x")` + SymbolIndex | 0.99 | ADR-003 D.2 |
| `@FeignClient(name="ts-x")` | 0.99 | ADR-003 |
| RestTemplate + строковый литерал | 0.90 | ADR-003 A.1 |
| `@Value` + ENV_INDEX (точный) | 0.85 | ADR-003 A.3 |
| `@Value` + ENV_INDEX (fuzzy BGE-M3) | 0.65 | ADR-011 |
| `DiscoveryClient.getInstances("ts-x")` | 0.80 | ADR-003 A.4 |
| docker-compose `depends_on` | 0.40 | Coarse, только сервис-уровень |
| BGE-M3 по URL-строке | 0.55 | Только если имя сервиса нечёткое |
| `USES_MAGIC_CALL` | — | Last resort |

**`depends_on` как corroborating signal:** если code analysis уже нашёл `CALLS_SERVICE`, `depends_on` подтверждает ребро (повышает confidence). Если code analysis ничего не нашёл — создаёт ребро с conf=0.40 на уровне сервиса.

---

### Q4: Неизвестный сервис / директория — векторная разведка

Когда сервис не Java/Spring (Python, Go) или структура нестандартная:

```python
def bootstrap_unknown_service(service_dir: Path) -> ServiceBootstrap:
    """Быстрая разведка: структура → requirements → entrypoint."""
    signals = []

    # 1. requirements.txt / go.mod / package.json — быстро и надёжно
    if (req := service_dir / "requirements.txt").exists():
        deps = req.read_text(errors="replace").lower()
        for framework, conf in [("flask", 0.95), ("tornado", 0.95),
                                  ("fastapi", 0.95), ("django", 0.90)]:
            if framework in deps:
                signals.append(Signal(f"python_{framework}", conf, "requirements"))
                break

    if (go_mod := service_dir / "go.mod").exists():
        signals.append(Signal("go_stdlib", 0.80, "go_mod"))

    # 2. Имена директорий (структура проекта)
    dirs = {d.name.lower() for d in service_dir.rglob("*") if d.is_dir()}
    dir_text = " ".join(sorted(dirs))
    dir_role = classify_structure_vector(bge_m3.embed(dir_text))
    signals.append(Signal(dir_role, 0.55, "dir_structure_vector"))

    # 3. Первый файл (entrypoint): main.py, app.py, main.go, index.js
    for ep_name in ["main.py", "app.py", "main.go", "server.go", "index.js"]:
        if (ep := service_dir / ep_name).exists():
            snippet = ep.read_text(errors="replace")[:500]
            role = bge_m3_classify_snippet(snippet)
            signals.append(Signal(role, 0.60, f"entrypoint_{ep_name}"))
            break

    return ServiceBootstrap(fuse_signals(signals))
```

---

## Решение C: fuse_signals — слияние конфликтующих источников

```python
@dataclass
class Signal:
    value: Any
    conf: float       # 0.0–1.0
    source: str

@dataclass
class FusedResult:
    value: Any
    conf: float
    sources: list[str]
    contradictions: list[Signal] | None = None


def fuse_signals(signals: list[Signal], min_conf: float = 0.3) -> FusedResult:
    """Слить несколько сигналов об одном вопросе.

    Победитель — значение с наибольшей суммарной уверенностью.
    Противоречие от сильного источника (conf > 0.7) понижает итог на 20%.
    """
    if not signals:
        return FusedResult(value=None, conf=0.0, sources=["no_signal"])

    groups: dict[Any, list[Signal]] = defaultdict(list)
    for s in signals:
        groups[s.value].append(s)

    winner_value, winner_signals = max(
        groups.items(),
        key=lambda kv: sum(s.conf for s in kv[1])
    )
    fused_conf = min(1.0, sum(s.conf for s in winner_signals))

    contradictions = [s for s in signals if s.value != winner_value]
    if contradictions:
        max_contra = max(s.conf for s in contradictions)
        if max_contra > 0.7:
            # Сильное противоречие: понижаем уверенность
            fused_conf *= 0.8

    return FusedResult(
        value=winner_value,
        conf=fused_conf,
        sources=[s.source for s in winner_signals],
        contradictions=contradictions if contradictions else None
    )
```

---

## Решение D: Изоляция ошибок — правильная гранулярность

**Принцип:** ошибка не поднимается выше своего уровня.

```
REPO → SERVICE → FILE → CLASS → METHOD
```

На каждом уровне — `try/except`, при ошибке — минимальный узел + лог, не краш.

```python
class IsolatedParser:

    def parse_repo(self, repo_path: Path) -> RepoGraph:
        graph = RepoGraph()
        for service_dir in self._find_services(repo_path):
            try:
                svc = self.parse_service(service_dir)
                graph.add_service(svc)
            except Exception as e:
                graph.add_failed_service(service_dir.name, error=str(e))
                log.warning("service_parse_failed", service=service_dir.name, error=e)
        return graph

    def parse_service(self, service_dir: Path) -> ServiceGraph:
        svc = ServiceGraph(name=detect_service_name(service_dir))
        for src_file in iter_source_files(service_dir):
            # Исключаем тесты сразу по пути — не по содержимому
            if "/test/" in str(src_file) or "/tests/" in str(src_file):
                continue
            try:
                nodes = self.parse_file(src_file)
                svc.add_nodes(nodes)
            except ParseError as e:
                svc.add_partial_node(src_file, error=str(e))
            except Exception as e:
                svc.add_partial_node(src_file, error=f"unexpected:{e}")
        return svc

    def parse_file(self, file_path: Path) -> list[ParsedNode]:
        # errors="replace": UTF-8 с китайскими символами в комментариях не крашит
        text = file_path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ts_parser.parse(text.encode())
        except Exception as e:
            raise ParseError(f"tree-sitter: {e}") from e

        nodes = []
        for class_node in find_class_nodes(tree):
            try:
                nodes.append(self._parse_class(class_node, text, file_path))
            except Exception:
                # Один сломанный класс — минимальный узел, продолжаем
                nodes.append(MinimalClassNode(
                    file_path=file_path,
                    parse_status="PARTIAL_PARSED"
                ))
        return nodes
```

**Ключевые решения:**
- `errors="replace"` — любые байты в файле, включая китайские комментарии в train-ticket
- `"/test/"` — фильтр по пути, не по содержимому (O(1) вместо парсинга)
- `PARTIAL_PARSED` узел всегда имеет `file_path` и `service` → BFS не потеряет стартовую точку
- YAML-ошибки изолируются от Java-парсинга (разные pipeline, разные try/except)
- Ошибка одного конфиг-файла не блокирует всю конфиг-фазу

---

## Решение E: Комментарии → метаданные deprecated и bug

Это **не** ADR-016 (shadow connections — закомментированный код). Это **быстрый проход** по однострочным комментариям для обогащения метаданных узлов.

```python
# Регексы: дешевле BGE-M3, достаточно для 95% случаев
DEPRECATION_PATTERNS = [
    r"@[Dd]eprecated",
    r"//\s*[Dd]eprecated",
    r"//\s*[Uu]se\s+\w+\s+instead",
    r"/\*\*.*?@deprecated.*?\*/",   # Javadoc @deprecated
    r"#\s*[Dd]eprecated",           # Python
]

BUG_PATTERNS = [
    r"//\s*(?:FIXME|BUG|HACK|XXX)[:\s]",
    r"#\s*(?:FIXME|BUG|HACK|XXX)[:\s]",
    r"//\s*[Ww]ork[- ]?around",
    r"TODO:\s*(?:remove|delete|fix|replace)",
]


@dataclass
class CommentMetadata:
    deprecated: bool = False
    deprecated_since: str | None = None
    has_bug_note: bool = False
    bug_description: str | None = None  # первые 80 символов FIXME


def extract_comment_metadata(file_text: str,
                             line_start: int, line_end: int) -> CommentMetadata:
    """Регекс-проход по комментариям в диапазоне строк узла.

    BGE-M3 вызывается только при длинных комментариях (> 3 строк) без regex-совпадения.
    """
    lines = file_text.splitlines()[line_start:line_end]
    comment_lines = [l for l in lines
                     if l.strip().startswith(("//", "#", "*", "/*"))]
    text = "\n".join(comment_lines)

    meta = CommentMetadata()

    if any(re.search(p, text, re.S) for p in DEPRECATION_PATTERNS):
        meta.deprecated = True
        if m := re.search(r"since\s+v?([\d.]+)", text, re.I):
            meta.deprecated_since = m.group(1)

    if any(re.search(p, text) for p in BUG_PATTERNS):
        meta.has_bug_note = True
        if m := re.search(r"(?:FIXME|BUG|HACK)[:\s]+(.{0,80})", text):
            meta.bug_description = m.group(1).strip()

    return meta
```

**Влияние на criticality (ADR-004):**

```python
def apply_comment_metadata_to_criticality(node: GraphNode,
                                           meta: CommentMetadata) -> float:
    base = node.criticality or 1.0
    if meta.deprecated:
        base *= 0.5    # устаревший код: менее критичен
    if meta.has_bug_note:
        base *= 1.3    # известный баг: выше риск при изменении
    return base
```

**В JSON-выводе:**
```json
{
  "node_id": "ts-order-service::order.OldOrderService",
  "deprecated": true,
  "deprecated_since": "2.0",
  "criticality": 0.5
}
```

**BGE-M3 для комментариев** вызывается только если:
- Комментарий > 3 строк И
- Regex ничего не нашёл И
- Комментарий на русском или английском (per ADR-001)

---

## Связи с другими ADR (что меняется)

| ADR | Изменение |
|---|---|
| ADR-001 | `IsolatedParser` вместо ad-hoc try/except; `errors="replace"`; `/test/` exclusion по пути |
| ADR-002 | `CommentMetadata` поля в атрибутах узла: `deprecated`, `deprecated_since`, `has_bug_note` |
| ADR-003 | `depends_on` как слабый corroborating signal; векторная разведка для Python/Go |
| ADR-004 | `CommentMetadata` → multiplicative adjustment к `criticality`; deprecated × 0.5, bug × 1.3 |
| ADR-011 | `EnvironmentProfile` определяет Tier → управляет активацией тактик |
| ADR-014 | `bootstrap_unknown_service()` использует BGE-M3 только для Tier 4 |
| ADR-018 | Тактики активируются через Rule Engine только для Tier ≥ 2 |

## Последствия

- `EnvironmentProfile` вычисляется один раз при старте индексации — ~10ms.
- `fuse_signals()` добавляет overhead только там, где несколько тактик активны (обычно ≤ 5 сигналов).
- Comment metadata — бесплатный проход: regex на уже прочитанном тексте файла.
- `PARTIAL_PARSED` узлы теперь **гарантированно** имеют минимальные поля для BFS.
- Изоляция ошибок означает: граф может быть неполным, но никогда не сломает JSON-вывод.
- `depends_on` как слабый сигнал может создать лишние сервис-уровневые рёбра — нужно отследить влияние на precision при калибровке.
