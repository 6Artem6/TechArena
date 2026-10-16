"""Единый интерфейс для всех внешних инструментов анализа.

Любой инструмент (semgrep, ctags, crossplane, cppcheck...) реализует ToolAdapter
и возвращает список Finding в едином формате. Потребитель не знает о конкретном
инструменте — только о типе находки и её данных.
"""
from __future__ import annotations

import json
import subprocess
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable


class FindingKind(str, Enum):
    """Виды находок — что нашли в коде или конфиге."""
    # Символы
    SYMBOL_DEF    = "SYMBOL_DEF"      # определение класса / функции / поля
    IMPORT        = "IMPORT"          # импорт / зависимость
    # Вызовы
    CALLS_SERVICE = "CALLS_SERVICE"   # HTTP-вызов другого сервиса
    HTTP_ENDPOINT = "HTTP_ENDPOINT"   # объявление HTTP-эндпоинта
    ASYNC_CALL    = "ASYNC_CALL"      # очередь / event (RabbitMQ, Kafka)
    CALLS_DB      = "CALLS_DB"        # обращение к БД
    # Конфиги
    CONFIG_VALUE  = "CONFIG_VALUE"    # конфигурационное значение (URL, host, queue)
    ENV_ACCESS    = "ENV_ACCESS"      # чтение переменной окружения
    SERVICE_DEF   = "SERVICE_DEF"     # объявление сервиса (docker-compose, k8s)
    PROXY_ROUTE   = "PROXY_ROUTE"     # nginx proxy_pass / gateway route
    # Метаданные
    TECH_DEBT     = "TECH_DEBT"       # обнаруженный технический долг
    FRAMEWORK     = "FRAMEWORK"       # обнаруженный фреймворк / язык


@dataclass
class Location:
    file: str
    line: int = 0
    col: int = 0
    end_line: int = 0
    end_col: int = 0


@dataclass
class Finding:
    """Единица информации из любого инструмента."""
    kind: FindingKind | str
    location: Location
    data: dict                         # специфичные поля — зависят от kind
    confidence: float = 1.0           # 0.0–1.0
    source_tool: str = ""             # "semgrep" / "ctags" / "crossplane" / ...
    language: str | None = None       # "java" / "python" / "go" / None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["kind"] = str(self.kind.value if isinstance(self.kind, FindingKind) else self.kind)
        return d


@dataclass
class AnalysisResult:
    """Вывод одного инструмента на одном таргете."""
    tool: str
    target: str                        # путь к файлу или директории
    language: str | None = None
    findings: list[Finding] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    duration_ms: int = 0
    metadata: dict = field(default_factory=dict)

    def to_json(self, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def to_dict(self) -> dict:
        return {
            "tool": self.tool,
            "target": self.target,
            "language": self.language,
            "findings": [f.to_dict() for f in self.findings],
            "errors": self.errors,
            "duration_ms": self.duration_ms,
            "metadata": self.metadata,
        }


@dataclass
class AnalysisOptions:
    """Параметры запуска инструмента."""
    languages: list[str] = field(default_factory=list)   # фильтр языков
    rules: list[str] = field(default_factory=list)       # пути к файлам правил
    max_findings: int = 10_000
    timeout_s: int = 60
    extra: dict = field(default_factory=dict)            # специфичные параметры


@runtime_checkable
class ToolAdapter(Protocol):
    """Протокол для всех инструментов анализа.

    Реализации: SemgrepAdapter, CtagsAdapter, NginxAdapter, ...
    Все возвращают AnalysisResult — один формат, независимо от инструмента.
    """

    @property
    def name(self) -> str:
        """Уникальное имя инструмента."""
        ...

    @property
    def supported_languages(self) -> frozenset[str]:
        """Языки которые умеет анализировать. {'*'} = любой."""
        ...

    @property
    def supported_configs(self) -> frozenset[str]:
        """Типы конфигов: {'nginx', 'dockerfile', 'yaml', ...}. Пустое = нет."""
        ...

    def is_available(self) -> bool:
        """Проверяет что инструмент установлен и работает."""
        ...

    def analyze(self, target: Path, options: AnalysisOptions) -> AnalysisResult:
        """Запускает анализ и возвращает унифицированный результат."""
        ...


class BaseAdapter:
    """Базовый класс с утилитами — не обязателен, но удобен."""

    def _run(self, cmd: list[str], timeout: int = 60) -> tuple[str, str, int]:
        """Запускает команду и возвращает (stdout, stderr, returncode)."""
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return result.stdout, result.stderr, result.returncode
        except subprocess.TimeoutExpired:
            return "", f"Timeout after {timeout}s", 124
        except FileNotFoundError:
            return "", f"Command not found: {cmd[0]}", 127

    def _timed(self, fn, *args, **kwargs) -> tuple[any, int]:
        """Возвращает (результат, время_мс)."""
        t0 = time.monotonic()
        result = fn(*args, **kwargs)
        return result, int((time.monotonic() - t0) * 1000)
