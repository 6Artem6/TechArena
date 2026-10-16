"""Python-парсер для Flask / Tornado / FastAPI сервисов (task 8c, task 14c).

Стратегия: regex по тексту файла (tree-sitter-python опционально).
Извлекает:
  - @app.route / @blueprint.route → HTTP_ENDPOINT
  - app.run(host=..., port=N) / uvicorn.run(port=N) → listen_port
  - 'http://ts-xxx-service:PORT' строковые литералы → CALLS_SERVICE
  - os.environ.get("TS_XXX_HOST", "ts-xxx-service") → CALLS_SERVICE (conf 0.75)
  - import requests / urllib → импорты

train-ticket Python-сервисы:
  ts-avatar-service  — Flask, port=17001, /api/v1/avatar
  ts-voucher-service — Tornado, порт из env, calls ts-order-service
"""
from __future__ import annotations

import re
from pathlib import Path

from .base import EndpointInfo, LanguageParser, ParsedFile

# ─── Regex ────────────────────────────────────────────────────────────────────

# Flask/FastAPI: @app.route('/path', methods=['GET', 'POST'])
_FLASK_ROUTE = re.compile(
    r'@\w+\.route\s*\(\s*["\']([^"\']+)["\'][^)]*methods\s*=\s*\[([^\]]+)\]',
)
# Flask без methods (по умолчанию GET)
_FLASK_ROUTE_SIMPLE = re.compile(
    r'@\w+\.route\s*\(\s*["\']([^"\']+)["\']',
)

# FastAPI: @app.get('/path') @app.post('/path') ...
_FASTAPI_ROUTE = re.compile(
    r'@\w+\.(get|post|put|delete|patch|options|head)\s*\(\s*["\']([^"\']+)["\']',
    re.IGNORECASE,
)

# Tornado: class FooHandler(tornado.web.RequestHandler) + def get/post/put/delete
_TORNADO_HANDLER_CLASS = re.compile(r'class\s+(\w+)\s*\(.*RequestHandler.*\)\s*:')
_TORNADO_METHOD = re.compile(r'^\s+def\s+(get|post|put|delete|patch)\s*\(self', re.MULTILINE)

# app.run(host="0.0.0.0", port=17001)
_APP_RUN_PORT = re.compile(
    r'app\.run\s*\([^)]*port\s*=\s*(\d+)',
)
# uvicorn.run("...", host=..., port=N) / socketio.run(app, port=N)
_UVICORN_PORT = re.compile(
    r'(?:uvicorn|socketio|tornado\.ioloop).*port\s*=\s*(\d+)',
)
# tornado listen / web.Run(":N")
_TORNADO_LISTEN = re.compile(
    r'(?:self\.listen|app\.listen|web\.Run)\s*\(\s*[\'":]?(\d+)',
)

# HTTP-литералы: 'http://ts-xxx-service:PORT'
_HTTP_LITERAL = re.compile(
    r"""['"](https?://(ts-[a-z0-9\-]+)(?::\d+)?(?:/[^'"]*)?)['""]"""
)

# os.environ.get("TS_XXX_HOST", "ts-xxx-service")  — дефолт = имя сервиса
_OS_ENV_DEFAULT = re.compile(
    r"""os\.environ\.get\s*\(\s*['"][^'"]+['"]\s*,\s*['"](ts-[a-z0-9\-]+)['"]""",
)

# import requests / import urllib / from requests import ...
_IMPORT_RE = re.compile(
    r"^(?:import|from)\s+([\w.]+)",
    re.MULTILINE,
)


class PythonParser:
    """Парсер Python-файлов для Flask / Tornado / FastAPI.

    Реализует LanguageParser Protocol.
    """

    supported_language: str = "python"

    def parse(self, path: Path, service: str) -> ParsedFile:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return ParsedFile(service=service, path=str(path), language="python", error=str(e))

        result = ParsedFile(service=service, path=str(path), language="python")
        result.module = str(path.stem)  # имя файла как module

        try:
            self._extract_routes(text, result)
            self._extract_calls(text, result)
            self._extract_imports(text, result)
            self._extract_port(text, result)
        except Exception as e:
            result.error = f"python_parser: {e}"

        return result

    # ─── Приватные методы ─────────────────────────────────────────────────────

    def _extract_routes(self, text: str, result: ParsedFile) -> None:
        seen: set[str] = set()

        # FastAPI декораторы (@app.get, @router.post, ...)
        for m in _FASTAPI_ROUTE.finditer(text):
            http_method, path = m.group(1).upper(), m.group(2)
            key = f"{http_method}:{path}"
            if key not in seen:
                seen.add(key)
                result.endpoints.append(EndpointInfo(path=path, method=http_method))

        # Flask с methods= (приоритетнее чем simple)
        for m in _FLASK_ROUTE.finditer(text):
            path = m.group(1)
            methods_raw = m.group(2)
            methods = [x.strip().strip("'\"") for x in methods_raw.split(",")]
            for method in methods:
                method = method.upper()
                key = f"{method}:{path}"
                if key not in seen:
                    seen.add(key)
                    result.endpoints.append(EndpointInfo(path=path, method=method))

        # Flask без methods (GET по умолчанию) — только если FastAPI не нашёл
        for m in _FLASK_ROUTE_SIMPLE.finditer(text):
            path = m.group(1)
            key = f"GET:{path}"
            if key not in seen:
                seen.add(key)
                result.endpoints.append(EndpointInfo(path=path, method="GET"))

        # Tornado: класс + методы
        for cls_m in _TORNADO_HANDLER_CLASS.finditer(text):
            handler_name = cls_m.group(1)
            # Ищем HTTP-методы в блоке после class
            block_start = cls_m.end()
            block = text[block_start:block_start + 500]
            for method_m in _TORNADO_METHOD.finditer(block):
                method = method_m.group(1).upper()
                # Путь из Tornado роутинга нет напрямую — используем handler name
                path = f"/{handler_name.lower().replace('handler', '')}"
                key = f"{method}:{path}"
                if key not in seen:
                    seen.add(key)
                    result.endpoints.append(
                        EndpointInfo(path=path, method=method, handler=handler_name)
                    )

    def _extract_calls(self, text: str, result: ParsedFile) -> None:
        seen: set[str] = set()

        for m in _HTTP_LITERAL.finditer(text):
            svc = m.group(2)
            if svc and svc not in seen:
                seen.add(svc)
                result.calls_services.append(svc)

        for m in _OS_ENV_DEFAULT.finditer(text):
            svc = m.group(1)
            if svc and svc not in seen:
                seen.add(svc)
                result.calls_services.append(svc)

    def _extract_imports(self, text: str, result: ParsedFile) -> None:
        result.imports = [m.group(1) for m in _IMPORT_RE.finditer(text)]

    def _extract_port(self, text: str, result: ParsedFile) -> None:
        for pattern in (_APP_RUN_PORT, _UVICORN_PORT, _TORNADO_LISTEN):
            m = pattern.search(text)
            if m:
                try:
                    result.listen_port = int(m.group(1))
                    return
                except ValueError:
                    pass
