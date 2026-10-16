"""Адаптер для semgrep — универсальный AST-matching для 30+ языков."""
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

# Встроенные правила по категориям находок
_BUILTIN_RULES_DIR = Path(__file__).parent.parent.parent.parent.parent / "rules" / "semgrep"

# Маппинг metavar-ключей из semgrep-правил → finding kind
_METAVAR_TO_KIND: dict[str, FindingKind] = {
    "calls_service": FindingKind.CALLS_SERVICE,
    "http_endpoint": FindingKind.HTTP_ENDPOINT,
    "async_call":    FindingKind.ASYNC_CALL,
    "calls_db":      FindingKind.CALLS_DB,
    "env_access":    FindingKind.ENV_ACCESS,
    "import":        FindingKind.IMPORT,
    "symbol_def":    FindingKind.SYMBOL_DEF,
}


class SemgrepAdapter(BaseAdapter):
    """Запускает semgrep и конвертирует JSON-вывод в список Finding.

    Semgrep поддерживает Java, Python, Go, JS, TS, Ruby, C, C++, PHP,
    Kotlin, Scala, Swift, Rust и др. — один движок для всех.
    """

    name = "semgrep"
    supported_languages = frozenset({"*"})  # универсальный
    supported_configs = frozenset()

    def is_available(self) -> bool:
        return shutil.which("semgrep") is not None

    def analyze(self, target: Path, options: AnalysisOptions) -> AnalysisResult:
        rules = options.rules or self._default_rules()

        cmd = [
            "semgrep",
            "--json",
            "--quiet",
            "--no-git-ignore",
            "--timeout", str(options.timeout_s),
            "--max-memory", "512",
        ]

        for rule in rules:
            cmd += ["--config", rule]

        if options.languages:
            for lang in options.languages:
                cmd += ["--include", f"*.{_LANG_TO_EXT.get(lang, lang)}"]

        cmd.append(str(target))

        stdout, stderr, returncode = self._run(cmd, timeout=options.timeout_s + 5)

        result = AnalysisResult(tool=self.name, target=str(target))

        if returncode not in (0, 1):  # semgrep возвращает 1 если есть находки
            result.errors.append(stderr.strip())
            return result

        try:
            data = json.loads(stdout)
        except json.JSONDecodeError as exc:
            result.errors.append(f"JSON parse error: {exc}")
            return result

        for err in data.get("errors", []):
            result.errors.append(err.get("message", str(err)))

        for match in data.get("results", []):
            finding = self._convert(match)
            if finding:
                result.findings.append(finding)

        result.metadata["semgrep_version"] = data.get("version", "")
        result.metadata["rules_parsed"] = len(data.get("rules", []))
        return result

    def _convert(self, match: dict) -> Finding | None:
        meta = match.get("extra", {}).get("metadata", {})

        # Тип находки из метаданных правила
        kind_str = meta.get("kind", "")
        kind = _METAVAR_TO_KIND.get(kind_str, FindingKind(kind_str) if kind_str else FindingKind.SYMBOL_DEF)

        loc = match.get("start", {})
        end = match.get("end", {})
        path = match.get("path", "")
        lang = match.get("extra", {}).get("language", None)

        # Metavariables → data (например $NAME, $URL, $SERVICE)
        metavars = match.get("extra", {}).get("metavariables", {})
        data = {k.lstrip("$").lower(): v.get("abstract_content", v.get("bind_by_name", ""))
                for k, v in metavars.items()}
        data["message"] = match.get("extra", {}).get("message", "")
        data["rule_id"] = match.get("check_id", "")
        data.update({k: v for k, v in meta.items() if k != "kind"})

        return Finding(
            kind=kind,
            location=Location(
                file=path,
                line=loc.get("line", 0),
                col=loc.get("col", 0),
                end_line=end.get("line", 0),
                end_col=end.get("col", 0),
            ),
            data=data,
            confidence=float(meta.get("confidence", 0.85)),
            source_tool=self.name,
            language=lang,
        )

    def _default_rules(self) -> list[str]:
        if _BUILTIN_RULES_DIR.exists():
            return [str(_BUILTIN_RULES_DIR)]
        # Fallback на встроенные реестры semgrep
        return ["p/default"]


_LANG_TO_EXT: dict[str, str] = {
    "java": "java", "python": "py", "go": "go",
    "javascript": "js", "typescript": "ts", "rust": "rs",
    "ruby": "rb", "kotlin": "kt", "csharp": "cs",
    "c": "c", "cpp": "cpp", "php": "php", "scala": "scala",
}
