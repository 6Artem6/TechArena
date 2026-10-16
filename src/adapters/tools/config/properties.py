"""Адаптер для *.properties — Spring application.properties и аналогов.

Формат: key=value или key: value (Java Properties).
Особая обработка: spring.datasource.url, spring.rabbitmq.host,
spring.redis.host, server.port, management.server.port.
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
from ._protocols import parse_url, scan_urls

# Spring-ключи → FindingKind + категория
_SPRING_KEY_HINTS: dict[str, FindingKind] = {
    "spring.datasource.url":       FindingKind.CALLS_DB,
    "spring.r2dbc.url":            FindingKind.CALLS_DB,
    "spring.rabbitmq.host":        FindingKind.ASYNC_CALL,
    "spring.rabbitmq.addresses":   FindingKind.ASYNC_CALL,
    "spring.kafka.bootstrap-servers": FindingKind.ASYNC_CALL,
    "spring.redis.host":           FindingKind.CALLS_DB,
    "spring.redis.url":            FindingKind.CALLS_DB,
    "spring.data.mongodb.uri":     FindingKind.CALLS_DB,
    "spring.data.elasticsearch.cluster-nodes": FindingKind.CALLS_DB,
    "spring.application.name":     FindingKind.SERVICE_DEF,
    "eureka.client.service-url.defaultZone": FindingKind.CALLS_SERVICE,
}


class PropertiesAdapter(BaseAdapter):
    name = "properties"
    supported_languages = frozenset()
    supported_configs = frozenset({"properties", "spring-properties"})

    def is_available(self) -> bool:
        return True  # stdlib only

    def analyze(self, target: Path, options: AnalysisOptions) -> AnalysisResult:
        result = AnalysisResult(tool=self.name, target=str(target))

        try:
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception as exc:
            result.errors.append(str(exc))
            return result

        # Собираем все key=value (включая многострочные \-continuation)
        props: list[tuple[int, str, str]] = []  # (lineno, key, value)
        buf_line = 0
        buf_key = ""
        buf_val = ""

        for lineno, raw in enumerate(lines, start=1):
            stripped = raw.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith("!"):
                buf_key = ""
                continue

            if buf_key and raw.endswith("\\"):
                buf_val += stripped.rstrip("\\").strip()
                continue
            elif buf_key:
                buf_val += stripped
                props.append((buf_line, buf_key, buf_val))
                buf_key = ""
                continue

            # Новая пара
            sep = "=" if "=" in stripped else (":" if ":" in stripped else None)
            if sep is None:
                continue

            key_raw, _, val_raw = stripped.partition(sep)
            key = key_raw.strip()
            val = val_raw.strip()

            if raw.rstrip().endswith("\\"):
                buf_line, buf_key, buf_val = lineno, key, val.rstrip("\\").strip()
            else:
                props.append((lineno, key, val))

        for lineno, key, val in props:
            # Проверяем специфичные Spring-ключи
            hint_kind = _SPRING_KEY_HINTS.get(key.lower())
            if hint_kind == FindingKind.SERVICE_DEF:
                result.findings.append(Finding(
                    kind=FindingKind.SERVICE_DEF,
                    location=Location(file=str(target), line=lineno),
                    data={"name": val, "type": "spring_application"},
                    confidence=1.0,
                    source_tool=self.name,
                ))
                continue

            # Значение — URL → parse_url для точного протокола
            if hint_kind or "://" in val or val.startswith("jdbc:"):
                parsed = parse_url(val)
                if parsed:
                    result.findings.append(Finding(
                        kind=hint_kind or parsed.kind,
                        location=Location(file=str(target), line=lineno),
                        data={
                            "key": key,
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
                    continue

            # Голый хост (spring.rabbitmq.host=rabbitmq — не URL)
            if hint_kind in (FindingKind.ASYNC_CALL, FindingKind.CALLS_DB):
                result.findings.append(Finding(
                    kind=hint_kind,
                    location=Location(file=str(target), line=lineno),
                    data={"key": key, "host": val},
                    confidence=0.9,
                    source_tool=self.name,
                ))
                continue

            # server.port / management.server.port
            if key in ("server.port", "management.server.port"):
                result.findings.append(Finding(
                    kind=FindingKind.CONFIG_VALUE,
                    location=Location(file=str(target), line=lineno),
                    data={"key": key, "port": val},
                    confidence=1.0,
                    source_tool=self.name,
                ))
                continue

            # Сканируем значение на URL (для всех остальных ключей)
            found_urls = scan_urls(val)
            if found_urls:
                for p in found_urls:
                    result.findings.append(Finding(
                        kind=p.kind,
                        location=Location(file=str(target), line=lineno),
                        data={
                            "key": key,
                            "protocol": p.protocol,
                            "host": p.host,
                            "port": p.port,
                            "url": p.raw,
                        },
                        confidence=0.85,
                        source_tool=self.name,
                    ))
            else:
                # Обычная конфиг-пара
                result.findings.append(Finding(
                    kind=FindingKind.CONFIG_VALUE,
                    location=Location(file=str(target), line=lineno),
                    data={"key": key, "value": val},
                    confidence=1.0,
                    source_tool=self.name,
                ))

        return result
