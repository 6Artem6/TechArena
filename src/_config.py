"""Загрузчик domain_config.yaml — единая точка входа для доменных констант.

Использование:
    from src._config import cfg_get, cfg_cache_dir, _ROOT

    prefixes = cfg_get("func_variant_suffixes", ["plan", "price"])
    cache = cfg_cache_dir() / "my_cache.pkl"
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

_ROOT        = Path(__file__).parent.parent
_CONFIG_PATH = Path(__file__).parent / "domain_config.yaml"
_cfg: dict[str, Any] | None = None


def cfg_cache_dir() -> Path:
    """Путь к директории кеша (переопределяется через TT_CACHE_DIR)."""
    return Path(os.getenv("TT_CACHE_DIR", str(_ROOT / "cache")))


def build_optional_re(key: str) -> "re.Pattern[str]":
    """Собрать regex для optional_service_patterns[key].

    Порядок поиска:
      1. cache/optional_patterns.json (сгенерированный Qwen офлайн)
      2. domain_config.yaml optional_service_patterns[key]
    Raise ValueError если ключ не найден нигде.
    """
    import re as _re

    # 1. Кеш (Qwen-generated)
    cache_file = cfg_cache_dir() / "optional_patterns.json"
    if cache_file.exists():
        try:
            import json as _json
            cached = _json.loads(cache_file.read_text(encoding="utf-8"))
            pat = cached.get(key)
            if pat:
                return _re.compile(pat, _re.I)
        except Exception:
            pass

    # 2. YAML fallback
    pat = cfg_get("optional_service_patterns", {}).get(key)
    if pat:
        return _re.compile(pat, _re.I)

    raise ValueError(f"optional_service_patterns.{key} не найден (ни в кеше, ни в YAML)")


def _load() -> dict[str, Any]:
    global _cfg
    if _cfg is not None:
        return _cfg
    if not _CONFIG_PATH.exists():
        _cfg = {}
    else:
        try:
            import yaml  # PyYAML — опциональная зависимость; без него — пустой конфиг
            with _CONFIG_PATH.open(encoding="utf-8") as f:
                _cfg = yaml.safe_load(f) or {}
        except Exception:
            _cfg = {}

    # Merge сгенерированного project_config.json поверх YAML.
    # Ключи из кеша имеют приоритет — YAML остаётся fallback.
    _project_cfg_path = cfg_cache_dir() / "project_config.json"
    if _project_cfg_path.exists():
        try:
            import json as _json
            _proj = _json.loads(_project_cfg_path.read_text(encoding="utf-8"))
            if isinstance(_proj, dict):
                _cfg.update(_proj)
        except Exception:
            pass

    return _cfg


def cfg_get(key: str, default: Any = None) -> Any:
    """Получить значение по ключу верхнего уровня; default если ключ отсутствует."""
    return _load().get(key, default)
