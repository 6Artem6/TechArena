"""Адаптер для файлов зависимостей — IMPORT-находки.

Поддерживает: requirements.txt, pom.xml, package.json, go.mod,
Cargo.toml, pyproject.toml, Gemfile, composer.json.

Из зависимостей можно понять: какую БД использует сервис (если есть
драйвер postgresql/redis/mongo), какой брокер (amqp/kafka), какой
фреймворк (spring-boot/flask/gin/actix).
"""
from __future__ import annotations

import json
import math
import re
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from ..base import (
    AnalysisOptions,
    AnalysisResult,
    BaseAdapter,
    Finding,
    FindingKind,
    Location,
)

# Паттерны зависимостей → подсказка о технологии
_TECH_HINTS: dict[str, str] = {
    # Базы данных
    "mysql": "db:mysql", "mysqlclient": "db:mysql", "pymysql": "db:mysql",
    "psycopg": "db:postgresql", "psycopg2": "db:postgresql", "asyncpg": "db:postgresql",
    "pg": "db:postgresql", "postgres": "db:postgresql",
    "pymongo": "db:mongodb", "mongodb": "db:mongodb",
    "redis": "db:redis", "redis-py": "db:redis", "aioredis": "db:redis",
    "elasticsearch": "db:elasticsearch",
    "cassandra-driver": "db:cassandra", "cassandra": "db:cassandra",
    # Брокеры
    "pika": "mq:rabbitmq", "aio-pika": "mq:rabbitmq",
    "amqplib": "mq:rabbitmq",
    "kafka-python": "mq:kafka", "confluent-kafka": "mq:kafka",
    "nats-py": "mq:nats", "asyncio-nats-client": "mq:nats",
    # Spring (Java)
    "spring-boot-starter-data-jpa": "db:jpa",
    "spring-boot-starter-data-redis": "db:redis",
    "spring-boot-starter-data-mongodb": "db:mongodb",
    "spring-boot-starter-amqp": "mq:rabbitmq",
    "spring-kafka": "mq:kafka",
    "mysql-connector-java": "db:mysql",
    "postgresql": "db:postgresql",
    # Node.js
    "mongoose": "db:mongodb", "mongodb": "db:mongodb",
    "ioredis": "db:redis", "kafkajs": "mq:kafka",
    "amqplib": "mq:rabbitmq",
    # Go
    "go-redis": "db:redis", "mongo-driver": "db:mongodb",
    "pq": "db:postgresql", "pgx": "db:postgresql",
    "segmentio/kafka-go": "mq:kafka", "streadway/amqp": "mq:rabbitmq",
    # Веб-фреймворки Python
    "flask": "web:flask", "flask-restful": "web:flask", "flask-restx": "web:flask",
    "fastapi": "web:fastapi", "starlette": "web:fastapi",
    "django": "web:django", "djangorestframework": "web:django",
    "gunicorn": "web:gunicorn", "uvicorn": "web:uvicorn", "hypercorn": "web:uvicorn",
    # Веб-фреймворки Node.js
    "express": "web:express", "koa": "web:koa", "fastify": "web:fastify",
    "hapi": "web:hapi",
    # Spring Web
    "spring-boot-starter-web": "web:spring",
    # Корневые записи — "якоря" для векторного поиска по вариантам
    # (spring-security-web, rabbitmq-client, gin-gonic/gin и т.п.)
    "spring": "web:spring",
    "rabbitmq": "mq:rabbitmq",
    "gin": "web:gin",
    "actix": "web:actix",
    "sqlalchemy": "db:sqlalchemy",
}

# ─── Векторный индекс для нечёткого поиска по n-граммам ──────────────────────


def _ngrams(text: str, n: int = 3) -> Counter:
    """Символьные n-граммы без разделителей (flask-login → flasklogin → fla/las/…)."""
    t = re.sub(r"[-_./]", "", text.lower())
    return Counter(t[i : i + n] for i in range(max(0, len(t) - n + 1)))


def _cosine(a: Counter, b: Counter) -> float:
    dot = sum(a[k] * b[k] for k in a if k in b)
    mag = math.sqrt(sum(v * v for v in a.values())) * math.sqrt(
        sum(v * v for v in b.values())
    )
    return dot / mag if mag else 0.0


# Предвычисленные n-грамм-векторы для всех ключей _TECH_HINTS
_HINT_VECTORS: list[tuple[str, Counter]] = [
    (pkg, _ngrams(pkg)) for pkg in _TECH_HINTS
]

# Минимальная cosine-схожесть для векторного матча (подобрана эмпирически)
_NGRAM_THRESHOLD = 0.40


def _hint_by_ngrams(key: str) -> str:
    """Нечёткий поиск по n-граммам — ближайший ключ в _TECH_HINTS."""
    qvec = _ngrams(key)
    if not qvec:
        return ""
    best_hint, best_sim = "", 0.0
    for pattern, pvec in _HINT_VECTORS:
        sim = _cosine(qvec, pvec)
        if sim > best_sim:
            best_sim, best_hint = sim, _TECH_HINTS[pattern]
    return best_hint if best_sim >= _NGRAM_THRESHOLD else ""


class DepsAdapter(BaseAdapter):
    """Парсит файлы зависимостей → IMPORT-находки с tech-подсказками."""

    name = "deps"
    supported_languages = frozenset()
    supported_configs = frozenset({
        "pip", "python-project", "npm", "go-mod", "cargo",
        "maven", "gradle", "gemfile", "composer",
    })

    def is_available(self) -> bool:
        return True  # stdlib only

    def analyze(self, target: Path, options: AnalysisOptions) -> AnalysisResult:
        result = AnalysisResult(tool=self.name, target=str(target))
        name = target.name.lower()

        if name == "requirements.txt":
            self._parse_requirements(target, result)
        elif name in ("pyproject.toml", "setup.cfg"):
            self._parse_pyproject(target, result)
        elif name == "package.json":
            self._parse_package_json(target, result)
        elif name == "go.mod":
            self._parse_go_mod(target, result)
        elif name == "cargo.toml":
            self._parse_cargo_toml(target, result)
        elif name == "pom.xml":
            self._parse_pom_xml(target, result)
        elif name in ("build.gradle", "build.gradle.kts"):
            self._parse_gradle(target, result)
        elif name == "gemfile":
            self._parse_gemfile(target, result)
        elif name == "composer.json":
            self._parse_composer_json(target, result)

        result.metadata["imports_count"] = len(result.findings)
        return result

    # ─── Форматы ──────────────────────────────────────────────────────────

    def _emit(self, result: AnalysisResult, file: str, line: int,
              pkg: str, version: str = "", ecosystem: str = "") -> None:
        hint = self._hint(pkg)
        result.findings.append(Finding(
            kind=FindingKind.IMPORT,
            location=Location(file=file, line=line),
            data={
                "package": pkg,
                "version": version,
                "ecosystem": ecosystem,
                "tech_hint": hint,
            },
            confidence=1.0,
            source_tool=self.name,
        ))

    def _hint(self, pkg: str) -> str:
        key = pkg.lower().replace("_", "-")
        # 1. Точное совпадение — O(1)
        if key in _TECH_HINTS:
            return _TECH_HINTS[key]
        # 2. Подстрока — ловит spring-boot-starter-*, mysql-connector-java и т.п.
        for pattern, hint in _TECH_HINTS.items():
            if pattern in key:
                return hint
        # 3. n-граммы — нечёткий матч по корню (flask-login→flask, spring-security→spring)
        return _hint_by_ngrams(key)

    def _parse_requirements(self, target: Path, result: AnalysisResult) -> None:
        for lineno, line in enumerate(target.read_text(errors="replace").splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("-"):
                continue
            m = re.match(r'^([A-Za-z0-9_\-\.]+)\s*([><=!~^].+)?', line)
            if m:
                self._emit(result, str(target), lineno, m.group(1), m.group(2) or "", "pip")

    def _parse_pyproject(self, target: Path, result: AnalysisResult) -> None:
        try:
            import tomllib  # Python 3.11+
        except ImportError:
            try:
                import tomli as tomllib  # pip install tomli
            except ImportError:
                result.errors.append("tomllib/tomli not available for pyproject.toml")
                return
        try:
            doc = tomllib.loads(target.read_text(errors="replace"))
        except Exception as exc:
            result.errors.append(str(exc))
            return
        deps = (doc.get("project", {}).get("dependencies", [])
                or doc.get("tool", {}).get("poetry", {}).get("dependencies", {}))
        if isinstance(deps, list):
            for i, dep in enumerate(deps):
                m = re.match(r'^([A-Za-z0-9_\-\.]+)', str(dep))
                if m:
                    self._emit(result, str(target), i + 1, m.group(1), "", "pip")
        elif isinstance(deps, dict):
            for i, (pkg, ver) in enumerate(deps.items()):
                if pkg.lower() != "python":
                    self._emit(result, str(target), i + 1, pkg, str(ver), "pip")

    def _parse_package_json(self, target: Path, result: AnalysisResult) -> None:
        try:
            doc = json.loads(target.read_text(errors="replace"))
        except Exception as exc:
            result.errors.append(str(exc))
            return
        all_deps = {
            **doc.get("dependencies", {}),
            **doc.get("devDependencies", {}),
            **doc.get("peerDependencies", {}),
        }
        for i, (pkg, ver) in enumerate(all_deps.items()):
            self._emit(result, str(target), i + 1, pkg, str(ver), "npm")

    def _parse_go_mod(self, target: Path, result: AnalysisResult) -> None:
        for lineno, line in enumerate(target.read_text(errors="replace").splitlines(), 1):
            line = line.strip()
            m = re.match(r'^(\S+)\s+(v[\d\.\-\+a-zA-Z]+)', line)
            if m:
                pkg = m.group(1).split("/")[-1]  # последний сегмент пути
                self._emit(result, str(target), lineno, pkg, m.group(2), "go")
                # Сохраняем полный путь для поиска подсказок
                full = m.group(1).lower()
                hint = self._hint(full.split("/")[-1]) or self._hint(full.split("/")[-2] if "/" in full else "")
                if hint and result.findings:
                    result.findings[-1].data["full_module"] = m.group(1)
                    result.findings[-1].data["tech_hint"] = hint

    def _parse_cargo_toml(self, target: Path, result: AnalysisResult) -> None:
        try:
            import tomllib
        except ImportError:
            try:
                import tomli as tomllib
            except ImportError:
                result.errors.append("tomllib/tomli not available")
                return
        try:
            doc = tomllib.loads(target.read_text(errors="replace"))
        except Exception as exc:
            result.errors.append(str(exc))
            return
        for section in ("dependencies", "dev-dependencies", "build-dependencies"):
            for i, (pkg, ver) in enumerate(doc.get(section, {}).items()):
                ver_str = ver if isinstance(ver, str) else (ver.get("version", "") if isinstance(ver, dict) else "")
                self._emit(result, str(target), i + 1, pkg, ver_str, "cargo")

    def _parse_pom_xml(self, target: Path, result: AnalysisResult) -> None:
        try:
            tree = ET.parse(str(target))
            root = tree.getroot()
        except Exception as exc:
            result.errors.append(f"pom.xml parse error: {exc}")
            return

        ns = {"m": "http://maven.apache.org/POM/4.0.0"}

        def _text(el, tag: str) -> str:
            # XML Element с 0 детьми — falsy в Python, поэтому явная проверка is not None
            child = el.find(f"m:{tag}", ns)
            if child is None:
                child = el.find(tag)
            return (child.text or "").strip() if child is not None else ""

        for i, dep in enumerate(root.findall(".//m:dependency", ns) or root.findall(".//dependency")):
            group = _text(dep, "groupId")
            artifact = _text(dep, "artifactId")
            version = _text(dep, "version")
            if not artifact and not group:
                continue
            pkg = artifact if artifact else f"{group}:{artifact}"
            self._emit(result, str(target), i + 1, pkg, version, "maven")
            # Полное имя для tech_hint
            full = f"{group}:{artifact}".lower()
            if result.findings:
                result.findings[-1].data["full_artifact"] = full
                hint = self._hint(artifact) or self._hint(full)
                if hint:
                    result.findings[-1].data["tech_hint"] = hint

    def _parse_gradle(self, target: Path, result: AnalysisResult) -> None:
        for lineno, line in enumerate(target.read_text(errors="replace").splitlines(), 1):
            # implementation 'group:artifact:version' или implementation("...")
            m = re.search(r'''(?:implementation|api|compile|testImplementation)\s*[\("']([^"']+)[\)"']''', line)
            if m:
                coord = m.group(1)
                parts = coord.split(":")
                pkg = parts[1] if len(parts) > 1 else parts[0]
                version = parts[2] if len(parts) > 2 else ""
                self._emit(result, str(target), lineno, pkg, version, "gradle")

    def _parse_gemfile(self, target: Path, result: AnalysisResult) -> None:
        for lineno, line in enumerate(target.read_text(errors="replace").splitlines(), 1):
            m = re.match(r'''gem\s+['"]([^'"]+)['"](?:.*,\s*['"]([^'"]+)['"])?''', line.strip())
            if m:
                self._emit(result, str(target), lineno, m.group(1), m.group(2) or "", "gem")

    def _parse_composer_json(self, target: Path, result: AnalysisResult) -> None:
        try:
            doc = json.loads(target.read_text(errors="replace"))
        except Exception as exc:
            result.errors.append(str(exc))
            return
        all_deps = {**doc.get("require", {}), **doc.get("require-dev", {})}
        for i, (pkg, ver) in enumerate(all_deps.items()):
            if pkg != "php":
                self._emit(result, str(target), i + 1, pkg, str(ver), "composer")
