"""Go-парсер для net/http / gin / web сервисов (task 8c).

Стратегия: regex по тексту файла.
Извлекает:
  - http.HandleFunc("/path", handler) → HTTP_ENDPOINT
  - web.Get("/path", handler) / web.Post(...) → HTTP_ENDPOINT
  - gin.GET("/path", handler) → HTTP_ENDPOINT
  - web.Run(":PORT") / http.ListenAndServe(":PORT", ...) → listen_port
  - http.Get("http://ts-xxx:PORT/...") → CALLS_SERVICE
  - "http://ts-xxx-service:PORT" строковые литералы → CALLS_SERVICE

train-ticket Go-сервисы:
  ts-news-service — github.com/hoisie/web, web.Get("/(.*)", hello) + web.Run("0.0.0.0:12862")
"""
from __future__ import annotations

import re
from pathlib import Path

from .base import EndpointInfo, LanguageParser, ParsedFile

# ─── Regex ────────────────────────────────────────────────────────────────────

# net/http: http.HandleFunc("/path", handler)
_HTTP_HANDLE = re.compile(
    r'http\.HandleFunc\s*\(\s*["\']([^"\']+)["\']',
)

# github.com/hoisie/web: web.Get("/path", fn) web.Post("/path", fn)
_WEB_ROUTE = re.compile(
    r'web\.(Get|Post|Put|Delete|Patch|Options)\s*\(\s*["\']([^"\']+)["\']',
    re.IGNORECASE,
)

# Gin: r.GET("/path", fn) / router.POST("/path", fn)
_GIN_ROUTE = re.compile(
    r'\w+\.(GET|POST|PUT|DELETE|PATCH|OPTIONS|HEAD)\s*\(\s*["\']([^"\']+)["\']',
)

# web.Run(":PORT") / web.Run("0.0.0.0:PORT")
_WEB_RUN = re.compile(r'web\.Run\s*\(\s*["\'](?:[^"\']*:)?(\d+)["\']')
# http.ListenAndServe(":PORT", ...)
_HTTP_LISTEN = re.compile(r'http\.ListenAndServe\s*\(\s*["\']:(\d+)["\']')

# HTTP-литералы: "http://ts-xxx:PORT"
_HTTP_LITERAL = re.compile(r'"(https?://(ts-[a-z0-9\-]+)(?::\d+)?(?:/[^"]*)?)"')

# import "..."
_IMPORT_RE = re.compile(r'^\s+"([\w./\-]+)"', re.MULTILINE)


class GoParser:
    """Парсер Go-файлов для net/http / gin / web.

    Реализует LanguageParser Protocol.
    """

    supported_language: str = "go"

    def parse(self, path: Path, service: str) -> ParsedFile:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return ParsedFile(service=service, path=str(path), language="go", error=str(e))

        result = ParsedFile(service=service, path=str(path), language="go")
        result.module = path.stem  # имя файла как module

        # Пакет из `package main` или `package xxx`
        pkg_m = re.search(r"^package\s+(\w+)", text, re.MULTILINE)
        if pkg_m:
            result.module = pkg_m.group(1)

        try:
            self._extract_routes(text, result)
            self._extract_calls(text, result)
            self._extract_imports(text, result)
            self._extract_port(text, result)
        except Exception as e:
            result.error = f"go_parser: {e}"

        return result

    # ─── Приватные методы ─────────────────────────────────────────────────────

    def _extract_routes(self, text: str, result: ParsedFile) -> None:
        seen: set[str] = set()

        for m in _HTTP_HANDLE.finditer(text):
            path = m.group(1)
            key = f"*:{path}"
            if key not in seen:
                seen.add(key)
                result.endpoints.append(EndpointInfo(path=path, method="*"))

        for m in _WEB_ROUTE.finditer(text):
            method, path = m.group(1).upper(), m.group(2)
            key = f"{method}:{path}"
            if key not in seen:
                seen.add(key)
                result.endpoints.append(EndpointInfo(path=path, method=method))

        for m in _GIN_ROUTE.finditer(text):
            method, path = m.group(1).upper(), m.group(2)
            key = f"{method}:{path}"
            if key not in seen:
                seen.add(key)
                result.endpoints.append(EndpointInfo(path=path, method=method))

    def _extract_calls(self, text: str, result: ParsedFile) -> None:
        seen: set[str] = set()
        for m in _HTTP_LITERAL.finditer(text):
            svc = m.group(2)
            if svc and svc not in seen:
                seen.add(svc)
                result.calls_services.append(svc)

    def _extract_imports(self, text: str, result: ParsedFile) -> None:
        result.imports = [m.group(1) for m in _IMPORT_RE.finditer(text)]

    def _extract_port(self, text: str, result: ParsedFile) -> None:
        for pattern in (_WEB_RUN, _HTTP_LISTEN):
            m = pattern.search(text)
            if m:
                try:
                    result.listen_port = int(m.group(1))
                    return
                except ValueError:
                    pass
