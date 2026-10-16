"""Адаптер для тест-файлов — Two-Phase извлечение caller→callee и API-контрактов.

Phase 1 — Locate: ripgrep (если доступен) или rglob → список тест-файлов.
Phase 2 — Extract: regex по каждому файлу:
  - Mockito.when(restTemplate.exchange("http://ts-X:PORT/path", HttpMethod.Y))
    → CALLS_SERVICE (conf=0.85, source=test_mock) на уровне сервис→сервис
  - mockMvc.perform(get/post/put/delete("/api/v1/path"))
    → HTTP_ENDPOINT (conf=0.90, source=test_hint) — подтверждение RouteIndex

Ограничения (ADR-025 §E):
  - @MockBean не используется в train-ticket — паттерн не реализован
  - Очереди: Queues.queueName не раскрывает имя → ASYNC_CALL не создаём
  - CALLS_SERVICE только уровня сервис→сервис, не метод→метод
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

# http://ts-order-service:12031/api/v1/orderservice/order/
# Захватывает: (хост:порт, путь)
_MOCK_URL_RE = re.compile(
    r'Mockito\.when\(\s*restTemplate\.exchange\(\s*'
    r'"(http://ts-[a-zA-Z0-9-]+:\d+)([^"]*)"'
)

# HttpMethod.GET / HttpMethod.POST / ... — ищем после URL в том же блоке
_HTTP_METHOD_RE = re.compile(r'HttpMethod\.([A-Z]+)')

# .perform(get("/api/v1/...")) или .perform(MockMvcRequestBuilders.post("/api/..."))
_MOCK_MVC_RE = re.compile(
    r'\.perform\(\s*(?:MockMvcRequestBuilders\.)?'
    r'(get|post|put|delete|patch)\s*\(\s*"(/[^"]+)"',
    re.IGNORECASE,
)

# Только файлы внутри src/test/ — не индексируем main-код
_TEST_PATH_MARKER = "src/test"

# Ширина окна контекста (символов) для поиска HttpMethod после URL
_CONTEXT_WINDOW = 300

# Достоверность (ADR-025 §E.6)
_CONF_TEST_MOCK = 0.85
_CONF_TEST_HINT = 0.90


class TestAdapter(BaseAdapter):
    """Two-Phase анализатор тест-файлов: caller→callee + MockMvc API-контракты.

    Не требует внешних инструментов — stdlib + optional ripgrep для Phase 1.
    """

    name = "test_analyzer"
    supported_languages = frozenset({"java"})
    supported_configs = frozenset()

    def is_available(self) -> bool:
        return True  # stdlib only

    def analyze(self, target: Path, options: AnalysisOptions) -> AnalysisResult:
        result = AnalysisResult(tool=self.name, target=str(target))

        # Phase 1: найти тест-файлы
        test_files = self._locate(target, options)
        result.metadata["test_files_found"] = len(test_files)

        if not test_files:
            return result

        # Phase 2: извлечь паттерны из каждого файла
        callee_set: set[tuple[str, str]] = set()  # (caller_svc, callee_svc)
        for tf in test_files:
            caller = self._caller_from_path(tf, target)
            self._extract(tf, caller, result, callee_set)

        result.metadata["unique_caller_callee_pairs"] = len(callee_set)
        return result

    # ------------------------------------------------------------------ Phase 1

    def _locate(self, target: Path, options: AnalysisOptions) -> list[Path]:
        """Найти тест-файлы через ripgrep или rglob."""
        if shutil.which("rg"):
            files = self._locate_rg(target, options.timeout_s)
            if files is not None:
                return files
        return self._locate_rglob(target)

    def _locate_rg(self, target: Path, timeout: int) -> list[Path] | None:
        """ripgrep: файлы, содержащие restTemplate.exchange или .perform(."""
        stdout, _, rc = self._run([
            "rg",
            "--files-with-matches",
            "--glob", "*/test/**/*.java",
            "-e", r"restTemplate\.exchange",
            "-e", r"\.perform\(",
            str(target),
        ], timeout=timeout)
        if rc not in (0, 1):
            return None
        paths = [Path(p.strip()) for p in stdout.splitlines() if p.strip()]
        # Страховка: только src/test/ пути
        return [p for p in paths if _TEST_PATH_MARKER in str(p)]

    def _locate_rglob(self, target: Path) -> list[Path]:
        """Fallback: рекурсивный поиск *Test*.java и *Tests*.java."""
        result: list[Path] = []
        for pattern in ("*Test.java", "*Tests.java", "*TestCase.java"):
            for p in target.rglob(pattern):
                if _TEST_PATH_MARKER in str(p) and "target" not in p.parts:
                    result.append(p)
        return result

    # ------------------------------------------------------------------ Phase 2

    def _extract(
        self,
        tf: Path,
        caller: str | None,
        result: AnalysisResult,
        callee_set: set[tuple[str, str]],
    ) -> None:
        try:
            text = tf.read_text(errors="replace")
        except OSError as exc:
            result.errors.append(f"read {tf}: {exc}")
            return

        self._extract_mock_urls(tf, text, caller, result, callee_set)
        self._extract_mock_mvc(tf, text, caller, result)

    def _extract_mock_urls(
        self,
        tf: Path,
        text: str,
        caller: str | None,
        result: AnalysisResult,
        callee_set: set[tuple[str, str]],
    ) -> None:
        """Mockito.when(restTemplate.exchange("http://ts-X:PORT/path", HttpMethod.Y))"""
        for m in _MOCK_URL_RE.finditer(text):
            host_port = m.group(1)   # http://ts-order-service:12031
            path      = m.group(2)   # /api/v1/orderservice/order/
            line      = text.count("\n", 0, m.start()) + 1

            # Извлечь сервис и порт
            without_scheme = host_port.replace("http://", "")
            if ":" in without_scheme:
                callee_host, port_str = without_scheme.rsplit(":", 1)
            else:
                callee_host, port_str = without_scheme, ""

            # HttpMethod в окне после URL-аргумента
            window = text[m.start(): m.start() + _CONTEXT_WINDOW]
            method_m = _HTTP_METHOD_RE.search(window)
            http_method = method_m.group(1) if method_m else "UNKNOWN"

            # Дедупликация на уровне сервиса (не записываем один edge дважды)
            pair = (caller or "", callee_host)
            callee_set.add(pair)

            result.findings.append(Finding(
                kind=FindingKind.CALLS_SERVICE,
                location=Location(file=str(tf), line=line),
                data={
                    "caller":      caller,
                    "callee":      callee_host,
                    "port":        int(port_str) if port_str.isdigit() else port_str,
                    "path":        path.rstrip(),
                    "http_method": http_method,
                    "url":         host_port + path,
                    "source":      "test_mock",
                },
                confidence=_CONF_TEST_MOCK,
                source_tool=self.name,
                language="java",
            ))

    def _extract_mock_mvc(
        self,
        tf: Path,
        text: str,
        caller: str | None,
        result: AnalysisResult,
    ) -> None:
        """mockMvc.perform(get/post/put/delete("/api/v1/..."))"""
        for m in _MOCK_MVC_RE.finditer(text):
            http_method = m.group(1).upper()
            path        = m.group(2)
            line        = text.count("\n", 0, m.start()) + 1

            result.findings.append(Finding(
                kind=FindingKind.HTTP_ENDPOINT,
                location=Location(file=str(tf), line=line),
                data={
                    "method":  http_method,
                    "path":    path,
                    "service": caller,
                    "source":  "test_hint",
                },
                confidence=_CONF_TEST_HINT,
                source_tool=self.name,
                language="java",
            ))

    # ------------------------------------------------------------------ Helpers

    def _caller_from_path(self, tf: Path, root: Path) -> str | None:
        """Определить сервис-вызывающий из пути тест-файла.

        ts-cancel-service/src/test/java/... → "ts-cancel-service"
        Возвращает None если сегмент ts-* не найден.
        """
        try:
            rel = tf.relative_to(root)
        except ValueError:
            rel = tf
        for part in rel.parts:
            if part.startswith("ts-"):
                return part
        return None
