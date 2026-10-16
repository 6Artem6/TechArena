"""Базовые типы и протокол для всех языковых парсеров.

Каждый парсер реализует LanguageParser и возвращает ParsedFile.
GraphBuilder работает с ParsedFile через structural subtyping — он не
зависит от конкретного класса, только от наличия нужных полей.

Иерархия возвращаемых типов:
  ParsedFile        — базовый dataclass (python, go, js, ...)
  ParsedJavaFile    — отдельный dataclass с полем `package` и `classes`
  ParsedPythonFile  — ParsedFile + framework: str
  ParsedGoFile      — ParsedFile + framework: str
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass
class EndpointInfo:
    """HTTP-эндпоинт, объявленный в файле."""
    path: str
    method: str     # GET POST PUT DELETE PATCH *
    handler: str = ""   # имя функции / "ClassName.methodName"


@dataclass
class ParsedFile:
    """Унифицированный результат парсинга одного файла.

    Используется непосредственно для Python/Go/JS-файлов.
    Для Java — ParsedJavaFile (отдельный dataclass с `package` и `classes`).
    """
    service: str
    path: str
    language: str               # "python" / "go" / "js"
    module: str = ""            # модуль/пакет файла

    calls_services: list[str] = field(default_factory=list)
    endpoints: list[EndpointInfo] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    listen_port: int = 0        # из app.run(port=N) / web.Run(":N")
    error: str = ""


@runtime_checkable
class LanguageParser(Protocol):
    """Протокол для всех языковых парсеров.

    Реализации: JavaParser, PythonParser, GoParser.
    GraphBuilder хранит dict[lang → parser] и диспатчит по языку.
    """

    @property
    def supported_language(self) -> str:
        """Язык который парсит этот адаптер: "java" / "python" / "go"."""
        ...

    def parse(self, path: Path, service: str) -> ParsedFile:
        """Разобрать файл и вернуть ParsedFile (или совместимый тип)."""
        ...
