# TOOLS — Справочник инструментов анализа

Все инструменты работают через единый интерфейс `ToolAdapter` → `AnalysisResult` → JSON.  
Установка: `./setup.sh [путь-к-репо]`

---

## Универсальные инструменты (любой язык)

| Инструмент | Описание | Установка | `FindingKind` |
|---|---|---|---|
| **semgrep** | AST-matching для 30+ языков через YAML-правила | `pip install semgrep` | все виды |
| **universal-ctags** | Символьный индекс для 100+ языков, JSON-вывод | `brew install universal-ctags` | `SYMBOL_DEF` |
| **ripgrep** (`rg`) | Быстрый grep с типами файлов | `brew install ripgrep` | — (поиск) |
| **jq** | Обработка JSON-вывода в shell | `brew install jq` | — (CLI) |

---

## Java

| Инструмент | Описание | Tier | Установка |
|---|---|---|---|
| **tree-sitter-java** | AST-парсинг: классы, аннотации, вызовы | 1 | `git clone` (auto setup.sh) |
| **javalang** | Python-парсер Java как fallback при tree-sitter ошибках | 2 | `pip install javalang` |
| **semgrep** | Spring-правила: `@FeignClient`, `RestTemplate`, `@Value` | 2–3 | уже выше |
| **TestAdapter** | Two-Phase: `rg` → тест-файлы → `CALLS_SERVICE` (mock-URL, conf=0.85) + `HTTP_ENDPOINT` (MockMvc, conf=0.90). 35 caller→callee пар + 245 API-путей на train-ticket (ADR-025 §E) | 2 | stdlib |

---

## Python

| Инструмент | Описание | Tier | Установка |
|---|---|---|---|
| **tree-sitter-python** | AST-парсинг | 1 | `git clone` (auto) |
| **semgrep** | Flask/FastAPI/Django правила | 2–3 | уже выше |
| **ast** (stdlib) | Встроенный Python AST | 1 | не нужна |

---

## Go

| Инструмент | Описание | Tier | Установка |
|---|---|---|---|
| **tree-sitter-go** | AST-парсинг | 1 | `git clone` (auto) |
| **semgrep** | net/http правила | 2–3 | уже выше |

---

## JavaScript / TypeScript

| Инструмент | Описание | Tier | Установка |
|---|---|---|---|
| **tree-sitter-javascript** | AST-парсинг | 1 | `git clone` (auto) |
| **tree-sitter-typescript** | TSX + TS | 1 | `git clone` (auto) |
| **eslint** | Статический анализ JS/TS | 3 | `npm install -g eslint` |
| **semgrep** | React/Node.js правила | 2–3 | уже выше |

---

## C / C++

| Инструмент | Описание | Tier | Установка |
|---|---|---|---|
| **tree-sitter-c** | AST-парсинг C | 1 | `git clone` (auto) |
| **tree-sitter-cpp** | AST-парсинг C++ | 1 | `git clone` (auto) |
| **cppcheck** | Статический анализ, поиск ошибок | 3 | `brew install cppcheck` |
| **semgrep** | C/C++ правила | 2–3 | уже выше |
| **universal-ctags** | Символы из .c/.cpp/.h | 1 | уже выше |

---

## PHP

| Инструмент | Описание | Tier | Установка |
|---|---|---|---|
| **tree-sitter-php** | AST-парсинг | 1 | `git clone` (auto) |
| **phpstan** | Статический анализ PHP | 3 | `composer global require phpstan/phpstan` |
| **semgrep** | PHP правила | 2–3 | уже выше |

---

## Ruby / Kotlin / Scala / Rust

| Инструмент | Описание | Установка |
|---|---|---|
| **tree-sitter-ruby** | AST-парсинг Ruby | `git clone` (auto) |
| **tree-sitter-kotlin** | AST-парсинг Kotlin | `git clone` (auto) |
| **semgrep** | Универсальные правила | уже выше |

---

## Конфигурационные файлы

| Формат | Адаптер | `FindingKind` | Установка |
|---|---|---|---|
| **nginx.conf**, `*.nginx` | `NginxAdapter` (crossplane) | `PROXY_ROUTE`, `HTTP_ENDPOINT`, `SERVICE_DEF`, `CONFIG_VALUE` (listen) | `pip install crossplane` |
| **docker-compose.yml**, override | `YamlAdapter` | `SERVICE_DEF` + EXPOSE-порты, `ENV_ACCESS`, `CALLS_SERVICE` (depends_on) | `pip install pyyaml` |
| **kubernetes .yaml**, Helm templates | `YamlAdapter` | `SERVICE_DEF`, `CONFIG_VALUE` (ports), `ENV_ACCESS` | `pip install pyyaml` |
| **Chart.yaml** | `YamlAdapter` (helm-chart) | `SERVICE_DEF`, `IMPORT` (chart dependencies) | `pip install pyyaml` |
| **values.yaml**, `values-*.yaml` | `YamlAdapter` (helm-values) | `SERVICE_DEF` (image), `CONFIG_VALUE` (port), URLs → все протоколы | `pip install pyyaml` |
| **application.yml**, `bootstrap.yml` | `YamlAdapter` | `CALLS_DB` (datasource/redis), `ASYNC_CALL` (rabbitmq), `SERVICE_DEF` | `pip install pyyaml` |
| **application.properties**, `*.properties` | `PropertiesAdapter` | `CALLS_DB`, `ASYNC_CALL`, `SERVICE_DEF`, `CONFIG_VALUE` (server.port) | stdlib |
| **Dockerfile**, `Dockerfile.*` | `DockerAdapter` (dockerfile-parse) | `SERVICE_DEF` + EXPOSE-порты, `ENV_ACCESS`, URLs из ENV | `pip install dockerfile-parse` |
| **.env**, `.env.*` | `DotenvAdapter` | `ENV_ACCESS`, URLs → CALLS_SERVICE/CALLS_DB/ASYNC_CALL | stdlib |
| **requirements.txt**, `requirements-*.txt` | `DepsAdapter` | `IMPORT` + `tech_hint` (db:mysql, mq:rabbitmq, ...) | stdlib |
| **pom.xml** | `DepsAdapter` | `IMPORT` + `tech_hint` из artifactId | `xml.etree` stdlib |
| **package.json** | `DepsAdapter` | `IMPORT` + `tech_hint` | `json` stdlib |
| **go.mod** | `DepsAdapter` | `IMPORT` + `tech_hint` | stdlib |
| **Cargo.toml** | `DepsAdapter` | `IMPORT` + `tech_hint` | `tomllib` (Python 3.11+) |
| **pyproject.toml**, `setup.cfg` | `DepsAdapter` | `IMPORT` + `tech_hint` | `tomllib` stdlib |
| **Gemfile** | `DepsAdapter` | `IMPORT` | stdlib |
| **composer.json** | `DepsAdapter` | `IMPORT` | `json` stdlib |
| **build.gradle** | `DepsAdapter` | `IMPORT` + `tech_hint` | stdlib (regex) |
| **Terraform .tf** | — (TODO) | `SERVICE_DEF`, `CONFIG_VALUE` | `pip install python-hcl2` |
| **semgrep правила** | любой текстовый файл | зависит от правил | уже выше |

---

## Семантические / ML инструменты (Tier 3–4)

| Инструмент | Описание | Tier | Установка |
|---|---|---|---|
| **BGE-M3** | Многоязыковые эмбеддинги (1024-dim) | 3 | `pip install sentence-transformers` + `INSTALL_EMBEDDINGS=1 ./setup.sh` |
| **LanceDB** | Локальное векторное хранилище (Arrow-based) | 3 | `pip install lancedb` |
| **Ollama** | Локальные LLM-модели (CodeLlama, Mistral) | 4 | `brew install ollama` |

---

## Архитектура адаптеров

```
src/adapters/tools/
├── base.py             # FindingKind, Finding, AnalysisResult, ToolAdapter Protocol
├── registry.py         # ToolRegistry — детекция языка/конфига, выбор адаптеров
├── __init__.py
├── code/
│   ├── semgrep.py      # SemgrepAdapter — универсальный AST-matching (30+ языков)
│   ├── ctags.py        # CtagsAdapter — символьный индекс (100+ языков)
│   ├── test_adapter.py # TestAdapter — Two-Phase: rg→тест-файлы → mock-URL + MockMvc
│   └── __init__.py
└── config/
    ├── _protocols.py   # parse_url(), scan_urls() — 35+ протоколов (ws, jdbc:*, amqp, ...)
    ├── nginx.py        # NginxAdapter — crossplane → PROXY_ROUTE + listen-порты
    ├── yaml_adapter.py # YamlAdapter — docker-compose, k8s, Helm, application.yml
    ├── docker.py       # DockerAdapter — EXPOSE-порты → SERVICE_DEF.ports
    ├── dotenv.py       # DotenvAdapter — .env → ENV_ACCESS + URL-протоколы
    ├── properties.py   # PropertiesAdapter — Spring *.properties → CALLS_DB, ASYNC_CALL
    ├── deps.py         # DepsAdapter — requirements.txt/pom.xml/package.json/go.mod/... → IMPORT
    └── __init__.py
```

**Добавить новый инструмент:**
```python
from src.adapters.tools.base import BaseAdapter, AnalysisResult, FindingKind

class MyToolAdapter(BaseAdapter):
    name = "mytool"
    supported_languages = frozenset({"java", "python"})
    supported_configs = frozenset()

    def is_available(self) -> bool:
        return shutil.which("mytool") is not None

    def analyze(self, target: Path, options: AnalysisOptions) -> AnalysisResult:
        ...
        return AnalysisResult(tool=self.name, target=str(target), findings=[...])
```

**Единый JSON-вывод:**
```json
{
  "tool": "semgrep",
  "target": "/path/to/repo",
  "language": null,
  "findings": [
    {
      "kind": "CALLS_SERVICE",
      "location": {"file": "src/Main.java", "line": 42, "col": 0, "end_line": 42, "end_col": 80},
      "data": {"service": "ts-order-service", "url": "http://ts-order-service:12031"},
      "confidence": 0.85,
      "source_tool": "semgrep",
      "language": "java"
    }
  ],
  "errors": [],
  "duration_ms": 340,
  "metadata": {"rules_parsed": 12}
}
```
