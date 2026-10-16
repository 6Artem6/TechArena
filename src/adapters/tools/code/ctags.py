"""Адаптер для universal-ctags — символьный индекс для любого языка.

Ctags поддерживает 100+ языков и работает без AST-парсера.
Даёт список всех символов (классы, функции, поля, методы) с позициями.
Быстро: весь train-ticket (~600 файлов) < 1 секунды.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from ..base import (
    AnalysisOptions,
    AnalysisResult,
    BaseAdapter,
    Finding,
    FindingKind,
    Location,
)

# ctags kind → FindingKind
_CTAGS_KIND_MAP: dict[str, str] = {
    "class":     "CLASS",
    "interface": "INTERFACE",
    "method":    "METHOD",
    "function":  "METHOD",
    "field":     "FIELD",
    "member":    "FIELD",
    "variable":  "FIELD",
    "constant":  "FIELD",
    "namespace": "PACKAGE",
    "package":   "PACKAGE",
    "module":    "PACKAGE",
    "struct":    "CLASS",
    "enum":      "CLASS",
    "typedef":   "CLASS",
    "prototype": "METHOD",
    "import":    "IMPORT",
}


class CtagsAdapter(BaseAdapter):
    """Запускает universal-ctags --output-format=json и строит символьный индекс.

    Это быстрый первый проход перед tree-sitter: узнать какие символы
    существуют, где они определены, и построить базовый граф имён.
    """

    name = "ctags"
    supported_languages = frozenset({"*"})  # universal-ctags знает 100+ языков
    supported_configs = frozenset()

    def is_available(self) -> bool:
        if shutil.which("ctags") is None:
            return False
        # Проверяем что это именно universal-ctags (не exuberant)
        stdout, _, _ = self._run(["ctags", "--version"])
        return "Universal Ctags" in stdout or "universal" in stdout.lower()

    def analyze(self, target: Path, options: AnalysisOptions) -> AnalysisResult:
        cmd = [
            "ctags",
            "--output-format=json",
            "--fields=+n+K+l+S+p",  # line, kind, language, signature, scope
            "--extras=+q",           # qualified names (ClassName.methodName)
            "-R",                    # рекурсивно
            "--exclude=target",
            "--exclude=node_modules",
            "--exclude=.git",
            "--exclude=__pycache__",
            "--exclude=build",
            "--exclude=dist",
            "--exclude=venv",
        ]

        if options.languages:
            langs = ",".join(_LANG_TO_CTAGS.get(l, l.capitalize()) for l in options.languages)
            cmd += [f"--languages={langs}"]

        cmd += ["--output=-", str(target)]  # вывод в stdout (одна запись на строку)

        stdout, stderr, returncode = self._run(cmd, timeout=options.timeout_s)

        result = AnalysisResult(tool=self.name, target=str(target))

        if returncode not in (0, 1) and not stdout.strip():
            result.errors.append(stderr.strip())
            return result

        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Пропускаем мета-записи ctags
            if entry.get("_type") != "tag":
                continue

            finding = self._convert(entry)
            if finding:
                result.findings.append(finding)

        result.metadata["total_symbols"] = len(result.findings)
        return result

    def _convert(self, entry: dict) -> Finding | None:
        ctags_kind = entry.get("kind", "").lower()
        kind_str = _CTAGS_KIND_MAP.get(ctags_kind, "SYMBOL_DEF")

        return Finding(
            kind=FindingKind.SYMBOL_DEF,
            location=Location(
                file=entry.get("path", ""),
                line=entry.get("line", 0),
            ),
            data={
                "name": entry.get("name", ""),
                "kind": kind_str,
                "language": entry.get("language", ""),
                "scope": entry.get("scope", ""),
                "scope_kind": entry.get("scopeKind", ""),
                "signature": entry.get("signature", ""),
                "pattern": entry.get("pattern", ""),
            },
            confidence=1.0,
            source_tool=self.name,
            language=entry.get("language", "").lower() or None,
        )


_LANG_TO_CTAGS: dict[str, str] = {
    "java": "Java", "python": "Python", "go": "Go",
    "javascript": "JavaScript", "typescript": "TypeScript",
    "rust": "Rust", "ruby": "Ruby", "kotlin": "Kotlin",
    "csharp": "C#", "c": "C", "cpp": "C++", "php": "PHP",
    "scala": "Scala", "swift": "Swift", "lua": "Lua",
    "shell": "Sh",
}
