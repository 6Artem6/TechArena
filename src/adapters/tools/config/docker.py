"""Адаптер для Dockerfile — парсинг через dockerfile-parse.

dockerfile-parse читает инструкции: FROM, ENV, EXPOSE, CMD, ENTRYPOINT.
EXPOSE-порты добавляются в SERVICE_DEF.data["ports"] — так потребитель
видит порты сервиса даже если они не продублированы в docker-compose.
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


class DockerAdapter(BaseAdapter):
    """Парсит Dockerfile через dockerfile-parse → ENV_ACCESS, SERVICE_DEF."""

    name = "dockerfile"
    supported_languages = frozenset()
    supported_configs = frozenset({"dockerfile"})

    def is_available(self) -> bool:
        try:
            from dockerfile_parse import DockerfileParser  # noqa: F401
            return True
        except ImportError:
            return False

    def analyze(self, target: Path, options: AnalysisOptions) -> AnalysisResult:
        from dockerfile_parse import DockerfileParser

        result = AnalysisResult(tool=self.name, target=str(target))

        try:
            dfp = DockerfileParser(path=str(target.parent), fileobj=None)
            dfp.fileobj = target.open("rb")
        except Exception as exc:
            result.errors.append(f"Dockerfile open error: {exc}")
            return result

        try:
            structure = dfp.structure or []

            # Первый проход: собираем все EXPOSE-порты (могут идти после FROM)
            expose_ports: list[dict] = []
            for instruction in structure:
                if instruction.get("instruction") == "EXPOSE":
                    for raw_port in instruction.get("value", "").split():
                        # Формат: 8080 или 8080/tcp или 8080/udp
                        port_num_str = raw_port.split("/")[0]
                        proto_hint = raw_port.split("/")[1] if "/" in raw_port else "tcp"
                        try:
                            expose_ports.append({
                                "port": int(port_num_str),
                                "transport": proto_hint,
                                "raw": raw_port,
                                "line": instruction.get("startline", 0) + 1,
                            })
                        except ValueError:
                            expose_ports.append({"port": raw_port, "transport": proto_hint, "raw": raw_port})

            # Базовый образ → SERVICE_DEF (с портами из EXPOSE)
            base_image = dfp.baseimage or ""
            if base_image:
                service_name = self._name_from_image(base_image, target.parent.name)
                result.findings.append(Finding(
                    kind=FindingKind.SERVICE_DEF,
                    location=Location(file=str(target), line=1),
                    data={
                        "name": service_name,
                        "image": base_image,
                        "type": "dockerfile",
                        "ports": expose_ports,          # EXPOSE прямо в сервисе
                    },
                    confidence=0.9,
                    source_tool=self.name,
                ))

            # ENV → ENV_ACCESS + сканируем значения на URL-протоколы
            envs = dfp.envs or {}
            for key, val in envs.items():
                result.findings.append(Finding(
                    kind=FindingKind.ENV_ACCESS,
                    location=Location(file=str(target)),
                    data={"key": key, "value": val, "source": "Dockerfile ENV"},
                    confidence=1.0,
                    source_tool=self.name,
                ))
                # Значение ENV может содержать connection URL
                for parsed in scan_urls(str(val)):
                    result.findings.append(Finding(
                        kind=parsed.kind,
                        location=Location(file=str(target)),
                        data={
                            "key": key,
                            "protocol": parsed.protocol,
                            "host": parsed.host,
                            "port": parsed.port,
                            "url": parsed.raw,
                            "category": parsed.category,
                            "source": "Dockerfile ENV",
                        },
                        confidence=0.9,
                        source_tool=self.name,
                    ))

            # Второй проход: EXPOSE как отдельные CONFIG_VALUE (для детализации)
            for ep in expose_ports:
                result.findings.append(Finding(
                    kind=FindingKind.CONFIG_VALUE,
                    location=Location(file=str(target), line=ep.get("line", 0)),
                    data={"port": ep["port"], "transport": ep["transport"], "directive": "EXPOSE"},
                    confidence=1.0,
                    source_tool=self.name,
                ))

            # CMD / ENTRYPOINT
            for instruction in structure:
                if instruction.get("instruction") in ("CMD", "ENTRYPOINT"):
                    result.findings.append(Finding(
                        kind=FindingKind.CONFIG_VALUE,
                        location=Location(file=str(target), line=instruction.get("startline", 0) + 1),
                        data={
                            "directive": instruction["instruction"],
                            "value": instruction.get("value", ""),
                        },
                        confidence=0.9,
                        source_tool=self.name,
                    ))

        except Exception as exc:
            result.errors.append(f"Dockerfile parse error: {exc}")
        finally:
            try:
                dfp.fileobj.close()
            except Exception:
                pass

        return result

    def _name_from_image(self, image: str, fallback: str) -> str:
        """Имя сервиса: из директории Dockerfile (ts-xxx), иначе из образа."""
        # Директория содержит канонический имя сервиса (ts-travel-service, etc.)
        if fallback and fallback not in (".", "", "src", "docker", "build", "app"):
            return fallback
        base = image.split(":")[0].split("/")[-1]
        return base or fallback
