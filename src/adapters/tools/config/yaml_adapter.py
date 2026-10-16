"""Адаптер для YAML-конфигов: docker-compose, kubernetes, application.yml.

Без сторонних библиотек (кроме PyYAML — уже в requirements).
Обнаруживает сервисы, порты, переменные среды, образы.
Все URL сканируются через _protocols — http, ws, mysql, redis, amqp, jdbc:*, ...

Исправления:
  - Multi-document YAML (k8s svc.yaml с ---): yaml.safe_load_all()
  - Spring ${VAR:default} → резолвим defaults перед parse_url()
  - Кастомные *.url ключи в application.yml → CALLS_SERVICE
"""
from __future__ import annotations

import re
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

# Паттерн Spring Boot: ${VAR_NAME:default_value}
_SPRING_VAR_RE = re.compile(r'\$\{[^:}]+:([^}]+)\}')


def _resolve_spring_defaults(url: str) -> str:
    """Заменить ${VAR:default} → default (Spring Boot pattern)."""
    return _SPRING_VAR_RE.sub(lambda m: m.group(1), url)


class YamlAdapter(BaseAdapter):
    """Парсит docker-compose.yml, kubernetes .yaml, application.yml через PyYAML."""

    name = "yaml"
    supported_languages = frozenset()
    supported_configs = frozenset({
        "docker-compose", "kubernetes", "yaml", "k8s",
        "helm-values", "helm-chart",   # Helm Chart.yaml и values.yaml
    })

    def is_available(self) -> bool:
        try:
            import yaml  # noqa: F401
            return True
        except ImportError:
            return False

    def analyze(self, target: Path, options: AnalysisOptions) -> AnalysisResult:
        import yaml

        result = AnalysisResult(tool=self.name, target=str(target))

        try:
            text = target.read_text(encoding="utf-8", errors="replace")
            # safe_load_all поддерживает multi-document YAML (k8s манифесты с ---)
            # и корректно работает для single-document файлов тоже
            docs = [d for d in yaml.safe_load_all(text) if isinstance(d, dict)]
        except Exception as exc:
            result.errors.append(f"YAML parse error: {exc}")
            return result

        if not docs:
            return result

        fname = target.name.lower()
        for doc in docs:
            self._process_doc(doc, fname, str(target), result)

        return result

    def _process_doc(self, doc: dict, fname: str, file_path: str, result: AnalysisResult) -> None:
        """Определить тип YAML-документа и обработать его."""
        if "services" in doc or (fname.startswith("docker-compose") and "version" in doc):
            self._parse_docker_compose(doc, file_path, result)
        elif doc.get("kind") in ("Deployment", "Service", "StatefulSet", "DaemonSet",
                                  "Pod", "Ingress", "ConfigMap", "Secret",
                                  "HorizontalPodAutoscaler", "CronJob", "Job"):
            self._parse_k8s(doc, file_path, result)
        elif doc.get("apiVersion") == "v2" and "name" in doc and "version" in doc:
            # Chart.yaml (Helm)
            self._parse_helm_chart(doc, file_path, result)
        elif fname in ("values.yaml", "values.yml") or fname.startswith("values"):
            # Helm values — сканируем как generic + смотрим image/service блоки
            self._parse_helm_values(doc, file_path, result)
        elif "spring" in doc or "server" in doc or "management" in doc:
            self._parse_application_yml(doc, file_path, result)
        else:
            self._scan_generic(doc, file_path, result)

    def _parse_docker_compose(self, doc: dict, file_path: str, result: AnalysisResult) -> None:
        services = doc.get("services", {})
        if not isinstance(services, dict):
            return

        for svc_name, svc_conf in services.items():
            if not isinstance(svc_conf, dict):
                continue

            result.findings.append(Finding(
                kind=FindingKind.SERVICE_DEF,
                location=Location(file=file_path),
                data={
                    "name": svc_name,
                    "image": svc_conf.get("image", ""),
                    "ports": svc_conf.get("ports", []),
                    "type": "docker-compose",
                },
                confidence=1.0,
                source_tool=self.name,
            ))

            # ENV переменные сервиса
            env = svc_conf.get("environment", {})
            if isinstance(env, list):
                env = dict(e.split("=", 1) if "=" in e else (e, "") for e in env)
            if isinstance(env, dict):
                for key, val in env.items():
                    result.findings.append(Finding(
                        kind=FindingKind.ENV_ACCESS,
                        location=Location(file=file_path),
                        data={"service": svc_name, "key": key, "value": str(val)},
                        confidence=1.0,
                        source_tool=self.name,
                    ))

            # depends_on → CALLS_SERVICE (слабый сигнал)
            deps = svc_conf.get("depends_on", [])
            if isinstance(deps, dict):
                deps = list(deps.keys())
            for dep in (deps if isinstance(deps, list) else []):
                result.findings.append(Finding(
                    kind=FindingKind.CALLS_SERVICE,
                    location=Location(file=file_path),
                    data={"caller": svc_name, "callee": dep, "via": "depends_on"},
                    confidence=0.6,
                    source_tool=self.name,
                ))

    def _parse_k8s(self, doc: dict, file_path: str, result: AnalysisResult) -> None:
        kind = doc.get("kind", "")
        meta = doc.get("metadata", {})
        name = meta.get("name", "")
        namespace = meta.get("namespace", "default")

        result.findings.append(Finding(
            kind=FindingKind.SERVICE_DEF,
            location=Location(file=file_path),
            data={"name": name, "namespace": namespace, "k8s_kind": kind},
            confidence=1.0,
            source_tool=self.name,
        ))

        # Для k8s Service — порты
        if kind == "Service":
            spec = doc.get("spec", {})
            for port_def in spec.get("ports", []):
                if isinstance(port_def, dict):
                    result.findings.append(Finding(
                        kind=FindingKind.CONFIG_VALUE,
                        location=Location(file=file_path),
                        data={
                            "service": name,
                            "port": port_def.get("port"),
                            "target_port": port_def.get("targetPort"),
                            "protocol": port_def.get("protocol", "TCP"),
                        },
                        confidence=1.0,
                        source_tool=self.name,
                    ))

        # ENV из container specs
        spec = doc.get("spec", {})
        template = spec.get("template", {})
        pod_spec = template.get("spec", spec)
        for container in pod_spec.get("containers", []):
            if not isinstance(container, dict):
                continue
            for env_entry in container.get("env", []):
                if isinstance(env_entry, dict):
                    val = env_entry.get("value", "")
                    val_from = env_entry.get("valueFrom", {})
                    if val_from:
                        val = f"<from:{list(val_from.keys())[0]}>"
                    result.findings.append(Finding(
                        kind=FindingKind.ENV_ACCESS,
                        location=Location(file=file_path),
                        data={
                            "service": name,
                            "key": env_entry.get("name", ""),
                            "value": str(val),
                        },
                        confidence=1.0,
                        source_tool=self.name,
                    ))

    def _parse_application_yml(self, doc: dict, file_path: str, result: AnalysisResult) -> None:
        spring = doc.get("spring", {})
        if not isinstance(spring, dict):
            return

        # spring.application.name
        app_name = spring.get("application", {}).get("name")
        if app_name:
            result.findings.append(Finding(
                kind=FindingKind.SERVICE_DEF,
                location=Location(file=file_path),
                data={"name": app_name, "type": "spring_application"},
                confidence=1.0,
                source_tool=self.name,
            ))

        # spring.datasource.url — через parse_url; разрешаем ${VAR:default}
        datasource = spring.get("datasource", {})
        if isinstance(datasource, dict) and datasource.get("url"):
            raw_url = datasource["url"]
            resolved_url = _resolve_spring_defaults(raw_url)
            parsed = parse_url(resolved_url)
            result.findings.append(Finding(
                kind=FindingKind.CALLS_DB,
                location=Location(file=file_path),
                data={
                    "url": raw_url,
                    "protocol": parsed.protocol if parsed else "jdbc",
                    "host": parsed.host if parsed else "",
                    "port": parsed.port if parsed else 0,
                    "db_name": parsed.db_name if parsed else "",
                    "driver": datasource.get("driver-class-name", ""),
                },
                confidence=1.0,
                source_tool=self.name,
            ))

        # spring.rabbitmq — явная структура (host/port отдельно, не URL)
        rabbitmq = spring.get("rabbitmq", {})
        if isinstance(rabbitmq, dict) and rabbitmq.get("host"):
            result.findings.append(Finding(
                kind=FindingKind.ASYNC_CALL,
                location=Location(file=file_path),
                data={
                    "broker": "rabbitmq",
                    "protocol": "amqp",
                    "host": rabbitmq["host"],
                    "port": rabbitmq.get("port", 5672),
                },
                confidence=1.0,
                source_tool=self.name,
            ))

        # spring.redis
        redis = spring.get("redis", {})
        if isinstance(redis, dict) and redis.get("host"):
            result.findings.append(Finding(
                kind=FindingKind.CALLS_DB,
                location=Location(file=file_path),
                data={
                    "broker": "redis",
                    "protocol": "redis",
                    "host": redis["host"],
                    "port": redis.get("port", 6379),
                },
                confidence=1.0,
                source_tool=self.name,
            ))

        # Кастомные *.url ключи — основные CALLS_SERVICE в Spring application.yml
        # Пример: train-service.url: http://${HOST:ts-train-service}:${PORT:14567}
        self._scan_spring_service_urls(doc, file_path, result)

        # Общий scan конфиг-значений (не-переменные URL)
        self._scan_generic(doc, file_path, result)

    def _scan_spring_service_urls(self, doc: dict, file_path: str, result: AnalysisResult) -> None:
        """Сканировать *.url ключи в application.yml — кастомные URL сервисов Spring."""
        self._flatten_service_urls(doc, "", file_path, result)

    def _flatten_service_urls(self, node, prefix: str, file_path: str, result: AnalysisResult) -> None:
        """Рекурсивно искать ключи url/address/endpoint с HTTP-значениями."""
        if isinstance(node, dict):
            for k, v in node.items():
                key_path = f"{prefix}.{k}" if prefix else k
                if (isinstance(v, str)
                        and k in ("url", "address", "endpoint", "baseUrl", "base-url")
                        and (v.startswith("http://") or v.startswith("https://"))):
                    resolved = _resolve_spring_defaults(v)
                    parsed = parse_url(resolved)
                    if parsed and not parsed.is_bind_all and parsed.kind == FindingKind.CALLS_SERVICE:
                        result.findings.append(Finding(
                            kind=FindingKind.CALLS_SERVICE,
                            location=Location(file=file_path),
                            data={
                                "key": key_path,
                                "url": v,           # оригинальный URL с переменными
                                "service": parsed.host,
                                "port": parsed.port,
                                "protocol": parsed.protocol,
                            },
                            confidence=0.85,
                            source_tool=self.name,
                        ))
                else:
                    self._flatten_service_urls(v, key_path, file_path, result)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                self._flatten_service_urls(v, f"{prefix}[{i}]", file_path, result)

    def _scan_generic(self, doc: dict, file_path: str, result: AnalysisResult) -> None:
        """Ищет URL-подобные строки во вложенной YAML-структуре."""
        self._flatten(doc, "", file_path, result)

    def _flatten(self, node, prefix: str, file_path: str, result: AnalysisResult) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                self._flatten(v, f"{prefix}.{k}" if prefix else k, file_path, result)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                self._flatten(v, f"{prefix}[{i}]", file_path, result)
        elif isinstance(node, str):
            # scan_urls находит ВСЕ протоколы: ws, wss, mysql, redis, amqp, jdbc:*, ...
            for parsed in scan_urls(node):
                result.findings.append(Finding(
                    kind=parsed.kind,
                    location=Location(file=file_path),
                    data={
                        "key": prefix,
                        "value": parsed.raw,
                        "protocol": parsed.protocol,
                        "host": parsed.host,
                        "port": parsed.port,
                        "category": parsed.category,
                    },
                    confidence=0.85,
                    source_tool=self.name,
                ))

    # ─── Helm ─────────────────────────────────────────────────────────────────

    def _parse_helm_chart(self, doc: dict, file_path: str, result: AnalysisResult) -> None:
        """Chart.yaml — метаданные чарта + зависимости."""
        result.findings.append(Finding(
            kind=FindingKind.SERVICE_DEF,
            location=Location(file=file_path),
            data={
                "name": doc.get("name", ""),
                "version": doc.get("version", ""),
                "app_version": doc.get("appVersion", ""),
                "type": "helm-chart",
            },
            confidence=1.0,
            source_tool=self.name,
        ))
        for dep in doc.get("dependencies", []):
            if isinstance(dep, dict):
                result.findings.append(Finding(
                    kind=FindingKind.IMPORT,
                    location=Location(file=file_path),
                    data={
                        "package": dep.get("name", ""),
                        "version": dep.get("version", ""),
                        "repository": dep.get("repository", ""),
                        "ecosystem": "helm",
                    },
                    confidence=1.0,
                    source_tool=self.name,
                ))

    def _parse_helm_values(self, doc: dict, file_path: str, result: AnalysisResult) -> None:
        """values.yaml — извлекаем image, ports, env и любые URL."""
        # image.repository + image.tag → SERVICE_DEF
        image_block = doc.get("image", {})
        if isinstance(image_block, dict) and image_block.get("repository"):
            result.findings.append(Finding(
                kind=FindingKind.SERVICE_DEF,
                location=Location(file=file_path),
                data={
                    "image": f"{image_block['repository']}:{image_block.get('tag', 'latest')}",
                    "type": "helm-values",
                },
                confidence=0.9,
                source_tool=self.name,
            ))

        # service.port / service.targetPort
        svc_block = doc.get("service", {})
        if isinstance(svc_block, dict) and svc_block.get("port"):
            result.findings.append(Finding(
                kind=FindingKind.CONFIG_VALUE,
                location=Location(file=file_path),
                data={
                    "port": svc_block["port"],
                    "target_port": svc_block.get("targetPort", svc_block["port"]),
                    "type": svc_block.get("type", "ClusterIP"),
                    "directive": "helm.service.port",
                },
                confidence=1.0,
                source_tool=self.name,
            ))

        # Общий scan для URL-значений (ingress.host, env.*, ...)
        self._scan_generic(doc, file_path, result)
