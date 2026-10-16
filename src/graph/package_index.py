"""PackageIndex — маппинг Java-пакетов на сервисы (task 8b).

Строится из Java-файлов репозитория: для каждого файла берётся
package-декларация и сопоставляется с сервисом из пути.

classify_import() различает три типа зависимостей:
  service  — import из другого сервиса (→ USES_DTO ребро)
  library  — edu.fudan.common.*, org.myproject.ms.monitoring.*
  external — javax.*, java.*, spring.*, ...

train-ticket специфика:
  - Пакеты короткие без `edu.fudan.xxx` префикса (travel.*, order.*, ...)
  - edu.fudan.common.* → ts-common (LIBRARY)
  - org.myproject.ms.monitoring.* → мониторинг (LIBRARY)
  - fdse.microservice.* → один из fdse-сервисов (LIBRARY)
"""
from __future__ import annotations

import re
from pathlib import Path

from ..core.walker import walk, service_name_of, EXCLUDED_DIRS

# Пакеты которые всегда LIBRARY (не наши сервисы)
_LIBRARY_ROOTS = frozenset({
    "java", "javax", "jakarta",
    "org.springframework", "org.springframework.boot",
    "org.slf4j", "org.apache", "org.junit", "org.mockito",
    "com.fasterxml", "io.swagger", "springfox",
    "edu.fudan.common",         # ts-common: shared entities/DTOs
    "org.myproject.ms.monitoring",  # мониторинг
    "fdse.microservice",        # fdse shared lib
    "com.netflix", "io.github.resilience4j",
    "reactor.core", "io.netty",
})

# Пакеты которые всегда EXTERNAL (третьестороннее, не влияет на blast radius)
_EXTERNAL_ROOTS = frozenset({
    "java", "javax", "jakarta",
    "org.junit", "org.mockito", "org.hamcrest",
    "lombok", "com.google",
})

_PACKAGE_RE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.MULTILINE)
_IMPORT_RE  = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)(?:\.\*)?;", re.MULTILINE)


class PackageIndex:
    """Индекс: package_root → service_name.

    Используется для classify_import() — определить тип зависимости по
    FQN импортируемого класса.
    """

    def __init__(self) -> None:
        # package_prefix (строка вида "travel" или "travel.service") → service
        self._pkg_to_service: dict[str, str] = {}
        # service → frozenset пакетных корней
        self._service_to_pkgs: dict[str, set[str]] = {}

    def build(self, repo_root: Path | str, langs: frozenset[str] | None = None) -> "PackageIndex":
        """Построить индекс из Java-файлов репозитория (читает файлы с диска)."""
        repo_root = Path(repo_root)
        for path, lang in walk(repo_root, langs or {"java"}):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            m = _PACKAGE_RE.search(text)
            if not m:
                continue
            pkg = m.group(1)
            service = service_name_of(path, repo_root)
            self._register(pkg, service)
        return self

    def build_from_parsed(self, parsed_files) -> "PackageIndex":
        """Построить индекс из уже распарсенных файлов (без повторного чтения).

        Поддерживает ParsedJavaFile (атрибут .package) и ParsedFile (.module).
        """
        for pf in parsed_files:
            pkg = getattr(pf, "package", None) or getattr(pf, "module", None) or ""
            if pkg:
                self._register(pkg, pf.service)
        return self

    def _register(self, pkg: str, service: str) -> None:
        """Зарегистрировать все префиксы пакета → сервис."""
        parts = pkg.split(".")
        for i in range(1, len(parts) + 1):
            prefix = ".".join(parts[:i])
            if prefix not in self._pkg_to_service:
                self._pkg_to_service[prefix] = service
        self._service_to_pkgs.setdefault(service, set()).add(parts[0])

    def classify_import(self, fqn: str) -> tuple[str, str | None]:
        """Классифицировать FQN импорта.

        Returns:
            ("library",  service_or_lib_name) — edu.fudan.common, spring, ...
            ("service",  service_name)         — другой ts-* сервис
            ("external", None)                 — java.*, javax.*, ...
            ("unknown",  None)                 — не удалось определить
        """
        if not fqn:
            return ("unknown", None)

        # Проверяем library-корни
        for lib_root in _LIBRARY_ROOTS:
            if fqn == lib_root or fqn.startswith(lib_root + "."):
                return ("library", lib_root)

        # Проверяем external-корни
        root = fqn.split(".")[0]
        if root in _EXTERNAL_ROOTS:
            return ("external", None)

        # Ищем в индексе — самый длинный подходящий префикс
        best_prefix = ""
        best_service = None
        for prefix, svc in self._pkg_to_service.items():
            if (fqn == prefix or fqn.startswith(prefix + ".")) and len(prefix) > len(best_prefix):
                best_prefix = prefix
                best_service = svc

        if best_service:
            return ("service", best_service)

        # Однословный пакет без точки и не в индексе — скорее всего external
        if "." not in fqn:
            return ("external", None)

        return ("unknown", None)

    def packages_for(self, service: str) -> frozenset[str]:
        """Все пакетные корни для сервиса."""
        return frozenset(self._service_to_pkgs.get(service, set()))

    def service_for_package(self, pkg: str) -> str | None:
        """Найти сервис по пакетному префиксу."""
        return self._pkg_to_service.get(pkg)

    @property
    def stats(self) -> dict[str, int]:
        return {
            "packages_indexed": len(self._pkg_to_service),
            "services_with_packages": len(self._service_to_pkgs),
        }
