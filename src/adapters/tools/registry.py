"""Реестр инструментов анализа.

ToolRegistry находит все доступные адаптеры, проверяет их наличие
и выбирает подходящие для конкретного таргета.
"""
from __future__ import annotations

import json
from pathlib import Path

from .base import AnalysisOptions, AnalysisResult, FindingKind, ToolAdapter


# Расширение → язык
EXTENSION_TO_LANGUAGE: dict[str, str] = {
    ".java": "java",
    ".py": "python",
    ".go": "go",
    ".js": "javascript",
    ".ts": "typescript",
    ".jsx": "javascript",
    ".tsx": "typescript",
    ".rs": "rust",
    ".rb": "ruby",
    ".kt": "kotlin",
    ".cs": "csharp",
    ".php": "php",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".scala": "scala",
    ".swift": "swift",
    ".lua": "lua",
    ".pl": "perl",
    ".sh": "shell",
    ".bash": "shell",
}

# Имя файла → тип конфига (точное совпадение, case-sensitive)
FILENAME_TO_CONFIG: dict[str, str] = {
    # nginx
    "nginx.conf": "nginx",
    "nginx": "nginx",
    # docker
    "docker-compose.yml": "docker-compose",
    "docker-compose.yaml": "docker-compose",
    "docker-compose.override.yml": "docker-compose",
    "docker-compose.override.yaml": "docker-compose",
    "Dockerfile": "dockerfile",
    # .env
    ".env": "dotenv",
    ".env.local": "dotenv",
    ".env.development": "dotenv",
    ".env.production": "dotenv",
    ".env.test": "dotenv",
    # Spring / Java
    "application.properties": "properties",
    "application.yml": "yaml",
    "application.yaml": "yaml",
    "bootstrap.properties": "properties",
    "bootstrap.yml": "yaml",
    # Helm
    "Chart.yaml": "helm-chart",
    "Chart.yml": "helm-chart",
    "values.yaml": "helm-values",
    "values.yml": "helm-values",
    # Зависимости
    "pom.xml": "maven",
    "build.gradle": "gradle",
    "build.gradle.kts": "gradle",
    "settings.gradle": "gradle",
    "package.json": "npm",
    "package-lock.json": "npm-lock",
    "yarn.lock": "npm-lock",
    "go.mod": "go-mod",
    "go.sum": "go-mod",
    "Cargo.toml": "cargo",
    "Cargo.lock": "cargo",
    "pyproject.toml": "python-project",
    "requirements.txt": "pip",
    "requirements-dev.txt": "pip",
    "setup.cfg": "python-project",
    "Gemfile": "gemfile",
    "Gemfile.lock": "gemfile",
    "composer.json": "composer",
    # CI/CD
    "Makefile": "makefile",
    "Jenkinsfile": "jenkins",
    ".gitlab-ci.yml": "gitlab-ci",
}

# Расширение → тип конфига
EXTENSION_TO_CONFIG: dict[str, str] = {
    ".nginx": "nginx",
    ".tf": "terraform",
    ".hcl": "terraform",
    ".properties": "properties",
    ".ini": "ini",
    ".cfg": "ini",
    ".conf": "conf",
}


def detect_language(path: Path) -> str | None:
    return EXTENSION_TO_LANGUAGE.get(path.suffix.lower())


def detect_config_type(path: Path) -> str | None:
    name = path.name
    name_lower = name.lower()

    # 1. Точное совпадение по имени файла (case-sensitive, как в реальных FS)
    if name in FILENAME_TO_CONFIG:
        return FILENAME_TO_CONFIG[name]

    # 2. Расширение
    if path.suffix.lower() in EXTENSION_TO_CONFIG:
        return EXTENSION_TO_CONFIG[path.suffix.lower()]

    # 3. Паттерны по имени файла (case-insensitive)
    if "nginx" in name_lower:
        return "nginx"
    if name_lower.startswith("docker-compose"):
        return "docker-compose"
    if name_lower == "dockerfile" or name_lower.startswith("dockerfile."):
        return "dockerfile"
    if name_lower.startswith(".env"):
        return "dotenv"
    # application-prod.yml, application-dev.properties, ...
    if name_lower.startswith("application-") or name_lower.startswith("bootstrap-"):
        if name_lower.endswith(".properties"):
            return "properties"
        if name_lower.endswith((".yml", ".yaml")):
            return "yaml"
    # Helm templates: templates/*.yaml → kubernetes
    if path.parent.name == "templates" and name_lower.endswith((".yml", ".yaml")):
        return "kubernetes"
    # Helm values-*.yaml → helm-values
    if name_lower.startswith("values") and name_lower.endswith((".yml", ".yaml")):
        return "helm-values"
    # requirements-*.txt
    if name_lower.startswith("requirements") and name_lower.endswith(".txt"):
        return "pip"
    # Generic YAML
    if name_lower.endswith((".yml", ".yaml")):
        return "yaml"
    return None


class ToolRegistry:
    """Реестр всех инструментов. Выбирает подходящие для таргета."""

    def __init__(self):
        self._adapters: list[ToolAdapter] = []
        self._available: list[ToolAdapter] = []

    def register(self, adapter: ToolAdapter) -> "ToolRegistry":
        self._adapters.append(adapter)
        if adapter.is_available():
            self._available.append(adapter)
        return self

    def available(self) -> list[ToolAdapter]:
        return list(self._available)

    def for_file(self, path: Path) -> list[ToolAdapter]:
        """Возвращает доступные инструменты для конкретного файла."""
        lang = detect_language(path)
        config_type = detect_config_type(path)

        result = []
        for adapter in self._available:
            can_handle = False
            if lang and ("*" in adapter.supported_languages or lang in adapter.supported_languages):
                can_handle = True
            if config_type and config_type in adapter.supported_configs:
                can_handle = True
            if can_handle:
                result.append(adapter)
        return result

    def for_repo(self, repo_root: Path) -> list[ToolAdapter]:
        """Все доступные инструменты, поддерживающие анализ директории."""
        return [a for a in self._available if "*" in a.supported_languages]

    def analyze_file(
        self,
        path: Path,
        options: AnalysisOptions | None = None,
    ) -> list[AnalysisResult]:
        """Запускает все подходящие инструменты на файле."""
        opts = options or AnalysisOptions()
        adapters = self.for_file(path)
        results = []
        for adapter in adapters:
            try:
                results.append(adapter.analyze(path, opts))
            except Exception as exc:
                results.append(AnalysisResult(
                    tool=adapter.name,
                    target=str(path),
                    errors=[str(exc)],
                ))
        return results

    def analyze_repo(
        self,
        repo_root: Path,
        options: AnalysisOptions | None = None,
    ) -> list[AnalysisResult]:
        """Запускает широкий анализ по всему репо."""
        opts = options or AnalysisOptions()
        results = []
        for adapter in self.for_repo(repo_root):
            try:
                results.append(adapter.analyze(repo_root, opts))
            except Exception as exc:
                results.append(AnalysisResult(
                    tool=adapter.name,
                    target=str(repo_root),
                    errors=[str(exc)],
                ))
        return results

    def status(self) -> dict:
        """JSON-статус всех инструментов — для диагностики."""
        return {
            "total": len(self._adapters),
            "available": len(self._available),
            "tools": [
                {
                    "name": a.name,
                    "available": a in self._available,
                    "languages": sorted(a.supported_languages),
                    "configs": sorted(a.supported_configs),
                }
                for a in self._adapters
            ],
        }
