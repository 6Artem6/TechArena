"""Файловый обходчик репозитория.

Рекурсивно обходит директорию, фильтрует шум, определяет язык/тип конфига.
Возвращает (path, kind) для каждого файла.

kind:
  "java" / "python" / "go" / "js" / "ts"  — исходный файл
  "config:<config_type>"                   — конфиг-файл (если config_detector передан)
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Iterator

from .._config import cfg_get

# Базовый набор — универсальные директории которые никогда не анализируем.
# Дополняется динамически при каждом вызове walk():
#   1) excluded_dirs из domain_config.yaml
#   2) простые имена директорий из ignore-файлов корня и вложенных директорий
EXCLUDED_DIRS = frozenset({
    "target", "node_modules", ".git", "__pycache__", "venv",
    "old-docs", ".mvn", "build", ".idea", "dist", ".gradle",
    "test-output", ".cache",
    "charts",
})

# Ignore-файлы, которые читаем в каждой директории при обходе
_IGNORE_FILENAMES = (".gitignore", ".dockerignore")

# Расширение → язык
_LANG_BY_EXT: dict[str, str] = {
    ".java": "java",
    ".py":   "python",
    ".go":   "go",
    ".js":   "js",
    ".ts":   "ts",
    ".tsx":  "ts",
}


def _read_ignore_files(dirpath: Path) -> set[str]:
    """Извлечь имена директорий из ignore-файлов в данной директории.

    Читает .gitignore и .dockerignore (если присутствуют).
    Берём только простые паттерны без wildcards и вложенных путей:
      "target/" → "target",  ".settings" → ".settings"
    Паттерны с *, ?, [ пропускаются — их нельзя сравнивать побайтово с именами.
    """
    dirs: set[str] = set()
    for fname in _IGNORE_FILENAMES:
        fpath = dirpath / fname
        if not fpath.exists():
            continue
        try:
            with fpath.open(encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "*" in line or "?" in line or "[" in line:
                        continue
                    name = line.rstrip("/")
                    # Только простые имена без путевых компонент
                    if "/" not in name and name:
                        dirs.add(name)
        except OSError:
            pass
    return dirs


def _build_global_excluded(root: Path) -> frozenset[str]:
    """Собрать глобальный (корневой) набор исключённых директорий.

    Объединяет базовые EXCLUDED_DIRS, excluded_dirs из config и ignore-файлы
    в корне репозитория. Эти паттерны применяются при входе в каждую директорию.
    Вложенные ignore-файлы читаются дополнительно прямо в walk().
    """
    cfg_dirs = set(cfg_get("excluded_dirs", []))
    root_ignore = _read_ignore_files(root)
    return EXCLUDED_DIRS | cfg_dirs | root_ignore


def walk(
    root: Path | str,
    langs: set[str] | None = None,
    *,
    config_detector: Callable[[Path], str | None] | None = None,
) -> Iterator[tuple[Path, str]]:
    """Обойти репозиторий и вернуть (path, kind) для каждого файла.

    Args:
        root:             корень репозитория
        langs:            фильтр по языкам; None = все поддерживаемые языки
        config_detector:  callable(path) → config_type | None; если передан,
                          конфиг-файлы тоже попадают в вывод с kind "config:<type>"

    Использует os.walk(topdown=True) для обрезки исключённых поддеревьев до спуска.
    """
    root = Path(root)
    global_excluded = _build_global_excluded(root)

    for dirpath_str, dirnames, filenames in os.walk(root, topdown=True):
        dirpath = Path(dirpath_str)

        # Вложенные ignore-файлы: читаем в каждой директории (кроме корня —
        # корневые уже учтены в global_excluded)
        local_excluded = _read_ignore_files(dirpath) if dirpath != root else set()
        effective_excluded = global_excluded | local_excluded

        # Обрезаем поддерево: убираем исключённые директории до рекурсии
        to_keep: list[str] = []
        for d in dirnames:
            if d in effective_excluded:
                continue
            # src/test/... — тесты не входят в граф производственного кода
            if d == "test" and dirpath.name == "src":
                continue
            to_keep.append(d)
        dirnames[:] = to_keep

        for fname in filenames:
            path = dirpath / fname
            lang = _LANG_BY_EXT.get(path.suffix.lower())
            if lang and (langs is None or lang in langs):
                yield path, lang
            elif config_detector is not None:
                config_type = config_detector(path)
                if config_type:
                    yield path, f"config:{config_type}"


def service_name_of(path: Path | str, root: Path | str) -> str:
    """Определить имя сервиса для файла по структуре директорий.

    Для train-ticket: первый компонент пути относительно root,
    если он начинается с 'ts-'. Иначе — имя директории верхнего уровня.

    Примеры:
      train-ticket/ts-travel-service/src/…/Foo.java  →  ts-travel-service
      train-ticket/ts-common/src/…/Bar.java           →  ts-common
    """
    path = Path(path)
    root = Path(root)
    try:
        rel = path.relative_to(root)
        return rel.parts[0]
    except (ValueError, IndexError):
        return path.parent.name
