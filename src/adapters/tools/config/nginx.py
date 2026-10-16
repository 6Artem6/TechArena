"""Адаптер для nginx.conf — парсинг через crossplane.

crossplane конвертирует nginx.conf в JSON-дерево директив.
Извлекаем proxy_pass (PROXY_ROUTE), location (HTTP_ENDPOINT),
upstream (SERVICE_DEF), root/alias (CONFIG_VALUE с путями к статике).
Порты и протоколы (ws, grpc, http) — через _protocols.
"""
from __future__ import annotations

import re
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
from ._protocols import parse_url

# Модификаторы location: нужно брать args[1] как паттерн, а не args[0]
_LOCATION_MODIFIERS = frozenset({"~*", "~", "=", "^~"})

# include-файлы которые не нужно открывать — стандартные nginx-файлы
_INCLUDE_NOISE = re.compile(
    r"mime\.types|fastcgi_params|proxy_params|koi-utf|koi-win|win-utf|scgi_params",
    re.IGNORECASE,
)


class NginxAdapter(BaseAdapter):
    """Парсит nginx.conf и nginx-фрагменты через crossplane → Finding.

    Находит:
    - proxy_pass → PROXY_ROUTE (целевой upstream-сервис + path)
    - location → HTTP_ENDPOINT (публичный маршрут)
    - upstream → SERVICE_DEF (backend-пул)
    """

    name = "nginx"
    supported_languages = frozenset()
    supported_configs = frozenset({"nginx"})

    def is_available(self) -> bool:
        try:
            import crossplane  # noqa: F401
            return True
        except ImportError:
            return False

    def analyze(self, target: Path, options: AnalysisOptions) -> AnalysisResult:
        import crossplane

        result = AnalysisResult(tool=self.name, target=str(target))

        try:
            parsed = crossplane.parse(str(target), combine=True, catch_errors=True)
        except Exception as exc:
            result.errors.append(f"crossplane parse error: {exc}")
            return result

        service_name = self._service_name_from_path(target)

        for doc in parsed.get("config", []):
            for err in doc.get("errors", []):
                msg = err.get("error", str(err)) if isinstance(err, dict) else str(err)
                # Подавляем ошибки стандартных include-файлов (mime.types и т.п.)
                if _INCLUDE_NOISE.search(msg):
                    result.metadata.setdefault("suppressed_include_errors", []).append(msg)
                else:
                    result.errors.append(msg)

            self._walk_block(doc.get("parsed", []), str(target), result, service_name)

        result.metadata["proxy_routes"] = sum(
            1 for f in result.findings if f.kind == FindingKind.PROXY_ROUTE
        )
        return result

    def _walk_block(
        self,
        directives: list,
        file_path: str,
        result: AnalysisResult,
        service_name: str,
    ) -> None:
        """Рекурсивный обход дерева nginx-директив."""
        location_stack: list[dict] = []  # {"path": ..., "modifier": ...}
        self._recurse(directives, file_path, result, location_stack, service_name)

    def _recurse(
        self,
        directives: list,
        file_path: str,
        result: AnalysisResult,
        location_stack: list[dict],
        service_name: str,
    ) -> None:
        for d in directives:
            directive = d.get("directive", "")
            args = d.get("args", [])
            line = d.get("line", 0)
            block = d.get("block", [])

            if directive == "location":
                # nginx синтаксис: location [modifier] pattern { ... }
                # args[0] — модификатор (~*, ~, =, ^~) или паттерн (если без модификатора)
                if len(args) >= 2 and args[0] in _LOCATION_MODIFIERS:
                    modifier = args[0]
                    location_path = args[1]
                else:
                    modifier = ""
                    location_path = args[0] if args else "/"

                new_stack = location_stack + [{"path": location_path, "modifier": modifier}]
                result.findings.append(Finding(
                    kind=FindingKind.HTTP_ENDPOINT,
                    location=Location(file=file_path, line=line),
                    data={
                        "path": location_path,
                        "modifier": modifier,   # "" | "~*" | "~" | "=" | "^~"
                        "method": "*",
                        "service": service_name,
                    },
                    confidence=0.95,
                    source_tool=self.name,
                ))
                if block:
                    self._recurse(block, file_path, result, new_stack, service_name)

            elif directive == "proxy_pass":
                upstream = args[0] if args else ""
                loc = location_stack[-1] if location_stack else {"path": "/", "modifier": ""}
                parsed = parse_url(upstream)
                service = parsed.host if parsed else self._extract_service_name(upstream)
                proto = parsed.protocol if parsed else "http"
                port = parsed.port if parsed else 0
                result.findings.append(Finding(
                    kind=FindingKind.PROXY_ROUTE,
                    location=Location(file=file_path, line=line),
                    data={
                        "location":          loc["path"],
                        "location_modifier": loc["modifier"],
                        "upstream":          upstream,
                        "service":           service,
                        "protocol":          proto,
                        "port":              port,
                    },
                    confidence=1.0,
                    source_tool=self.name,
                ))

            elif directive == "upstream":
                upstream_name = args[0] if args else ""
                upstream_servers = []
                for sub in (block or []):
                    if sub.get("directive") == "server" and sub.get("args"):
                        addr = sub["args"][0]
                        p = parse_url(f"http://{addr}") if "://" not in addr else parse_url(addr)
                        upstream_servers.append({
                            "address": addr,
                            "port": p.port if p else 0,
                        })
                result.findings.append(Finding(
                    kind=FindingKind.SERVICE_DEF,
                    location=Location(file=file_path, line=line),
                    data={
                        "name": upstream_name,
                        "type": "nginx_upstream",
                        "upstream_servers": upstream_servers,
                    },
                    confidence=1.0,
                    source_tool=self.name,
                ))
                if block:
                    self._recurse(block, file_path, result, location_stack, service_name)

            elif directive == "listen":
                # listen 80 / listen 443 ssl / listen [::]:80
                port_raw = args[0] if args else ""
                try:
                    port_num = int(port_raw.split(":")[-1])
                    is_ssl = "ssl" in args or "https" in args
                    result.findings.append(Finding(
                        kind=FindingKind.CONFIG_VALUE,
                        location=Location(file=file_path, line=line),
                        data={"directive": "listen", "port": port_num, "ssl": is_ssl},
                        confidence=1.0,
                        source_tool=self.name,
                    ))
                    # SERVICE_DEF для самого nginx: порт + имя сервиса из пути файла
                    if service_name:
                        result.findings.append(Finding(
                            kind=FindingKind.SERVICE_DEF,
                            location=Location(file=file_path, line=line),
                            data={
                                "name": service_name,
                                "type": "nginx",
                                "port": port_num,
                                "ssl": is_ssl,
                            },
                            confidence=0.9,
                            source_tool=self.name,
                        ))
                except (ValueError, IndexError):
                    pass

            elif directive in ("root", "alias"):
                # root /usr/share/nginx/html — путь к статическим файлам
                path_val = args[0] if args else ""
                if path_val:
                    loc_path = location_stack[-1]["path"] if location_stack else "/"
                    result.findings.append(Finding(
                        kind=FindingKind.CONFIG_VALUE,
                        location=Location(file=file_path, line=line),
                        data={
                            "directive":       directive,   # "root" или "alias"
                            "static_path":     path_val,
                            "for_location":    loc_path,
                        },
                        confidence=1.0,
                        source_tool=self.name,
                    ))

            elif directive in ("server", "http", "events", "stream"):
                if block:
                    self._recurse(block, file_path, result, location_stack, service_name)

            elif directive == "set":
                # set $variable value — для интерполяции proxy_pass
                if len(args) == 2 and args[0].startswith("$"):
                    result.findings.append(Finding(
                        kind=FindingKind.CONFIG_VALUE,
                        location=Location(file=file_path, line=line),
                        data={"variable": args[0], "value": args[1]},
                        confidence=0.8,
                        source_tool=self.name,
                    ))

    def _extract_service_name(self, upstream: str) -> str:
        """Из `http://ts-order-service:12031` извлекает `ts-order-service`."""
        if upstream.startswith("$"):
            return upstream
        for scheme in ("http://", "https://", "grpc://", "grpcs://", "ws://", "wss://"):
            if upstream.startswith(scheme):
                upstream = upstream[len(scheme):]
                break
        host = upstream.split("/")[0].split(":")[0]
        return host or upstream

    def _service_name_from_path(self, target: Path) -> str:
        """Определить имя сервиса из пути к nginx.conf.

        ts-ui-dashboard/nginx.conf → "ts-ui-dashboard"
        /etc/nginx/nginx.conf → "" (не сервисный файл)
        """
        for part in reversed(target.parts):
            if part.startswith("ts-"):
                return part
        # Fallback: директория прямо над файлом
        parent = target.parent.name
        if parent and parent not in ("nginx", "conf", "etc"):
            return parent
        return ""
