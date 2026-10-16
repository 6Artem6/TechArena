"""Загрузчик domain_config.yaml — единая точка входа для доменных констант.

Использование:
    from src._config import cfg_get

    prefixes = cfg_get("func_variant_suffixes", ["plan", "price"])
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

_CONFIG_PATH = Path(__file__).parent / "domain_config.yaml"
_cfg: dict[str, Any] | None = None


def _load() -> dict[str, Any]:
    global _cfg
    if _cfg is not None:
        return _cfg
    if not _CONFIG_PATH.exists():
        _cfg = {}
        return _cfg
    try:
        import yaml  # PyYAML — опциональная зависимость; без него — пустой конфиг
        with _CONFIG_PATH.open(encoding="utf-8") as f:
            _cfg = yaml.safe_load(f) or {}
    except Exception:
        _cfg = {}
    return _cfg


def cfg_get(key: str, default: Any = None) -> Any:
    """Получить значение по ключу верхнего уровня; default если ключ отсутствует."""
    return _load().get(key, default)
