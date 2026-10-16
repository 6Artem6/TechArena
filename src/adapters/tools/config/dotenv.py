"""Адаптер для .env файлов — переменные среды и connection URLs.

Формат: KEY=VALUE (комментарии #, пустые строки игнорируются).
Значения сканируются на протоколы через _protocols.
"""
from __future__ import annotations

from pathlib import Path

from ..base import (
    AnalysisOptions,
    AnalysisResult,
    BaseAdapter,
    Finding,
    FindingKind,
    Location,
)
from ._protocols import scan_urls


class DotenvAdapter(BaseAdapter):
    name = "dotenv"
    supported_languages = frozenset()
    supported_configs = frozenset({"dotenv"})

    def is_available(self) -> bool:
        return True  # stdlib only

    def analyze(self, target: Path, options: AnalysisOptions) -> AnalysisResult:
        result = AnalysisResult(tool=self.name, target=str(target))

        try:
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception as exc:
            result.errors.append(str(exc))
            return result

        for lineno, raw_line in enumerate(lines, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue

            # Убрать inline-комментарий
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")

            result.findings.append(Finding(
                kind=FindingKind.ENV_ACCESS,
                location=Location(file=str(target), line=lineno),
                data={"key": key, "value": val},
                confidence=1.0,
                source_tool=self.name,
            ))

            # Значение — connection URL (mysql://, redis://, amqp://, ...)
            for parsed in scan_urls(val):
                result.findings.append(Finding(
                    kind=parsed.kind,
                    location=Location(file=str(target), line=lineno),
                    data={
                        "env_key": key,
                        "protocol": parsed.protocol,
                        "host": parsed.host,
                        "port": parsed.port,
                        "db_name": parsed.db_name,
                        "category": parsed.category,
                        "url": parsed.raw,
                    },
                    confidence=1.0,
                    source_tool=self.name,
                ))

        return result
