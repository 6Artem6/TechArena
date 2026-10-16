"""Детектор SIBLING_OF рёбер по правилам именования сервисов.

Многоуровневый каскад (P-75):
  Шаг 1 (rule-based)   — суффиксы (-other, числовой, -vN, admin-, func) → точные пары
  Шаг 2 (fuzzy)        — trigram cosine ≥ 0.70 — нестандартные пары (inside/outside)
  Шаг 3 (векторный)    — BGE-M3 emb_struct ≥ 0.85 (только если шаги 1-2 недостаточны)

Правила (из ADR-027):
  other_variant   — ts-preserve-other-service ↔ ts-preserve-service       (0.85)
  numeric_variant — ts-travel2-service ↔ ts-travel-service                 (0.85)
  version_variant — ts-voucher-service-v2 ↔ ts-voucher-service             (0.85)
  admin_variant   — ts-admin-travel-service ↔ ts-travel-service            (0.60)
  func_variant    — ts-route-plan-service ↔ ts-route-service               (0.60)
                    (план, цена, доставка, карта, инфо)

Рёбра двусторонние: добавлять a→b и b→a.
depth_cost = 0 (SIBLING_OF не расходует бюджет глубины в BFS).
"""
from __future__ import annotations

import re

from .._config import cfg_get

# ─── Константы ────────────────────────────────────────────────────────────────

_VERSION_RE = re.compile(r"-v\d+$")

# Веса по типу связи (фиксированные — не являются доменной конфигурацией)
SIBLING_WEIGHTS: dict[str, float] = {
    "numeric_variant": 0.85,  # travel + travel2
    "other_variant":   0.85,  # preserve + preserve-other
    "version_variant": 0.85,  # voucher + voucher-v2
    "admin_variant":   0.60,  # travel + admin-travel
    "func_variant":    0.60,  # route + route-plan, food + food-delivery
}

# ─── Ленивый кеш для service_naming и func_variant_suffixes ───────────────────
_SVC_NAMING: tuple[str, str] | None = None
_FUNC_VARIANT_SUFFIXES_CACHE: frozenset[str] | None = None


def _get_svc_naming() -> tuple[str, str]:
    """Вернуть (prefix, suffix) из конфига."""
    global _SVC_NAMING
    if _SVC_NAMING is None:
        sn = cfg_get("service_naming", {})
        _SVC_NAMING = (sn.get("prefix", "ts-"), sn.get("suffix", "-service"))
    return _SVC_NAMING


def _get_func_suffixes() -> frozenset[str]:
    """Вернуть набор функциональных суффиксов из конфига."""
    global _FUNC_VARIANT_SUFFIXES_CACHE
    if _FUNC_VARIANT_SUFFIXES_CACHE is None:
        lst = cfg_get(
            "func_variant_suffixes",
            ["plan", "price", "delivery", "map", "info", "basic", "payment", "query"],
        )
        _FUNC_VARIANT_SUFFIXES_CACHE = frozenset(lst)
    return _FUNC_VARIANT_SUFFIXES_CACHE


# ─── Публичный API ────────────────────────────────────────────────────────────

def detect_siblings_by_rules(
    services: list[str],
) -> list[tuple[str, str, float, str]]:
    """Найти сиблингов в списке сервисов по правилам именования.

    Args:
        services: список имён сервисов (NodeId или строки вида "ts-xxx-service")

    Returns:
        Список (svc_a, svc_b, weight, reason) — ненаправленные пары.
        Каждая пара встречается ровно один раз.
    """
    prefix, _ = _get_svc_naming()
    ts_svcs = [s for s in services if s.startswith(prefix)]
    result: list[tuple[str, str, float, str]] = []
    seen: set[tuple[str, str]] = set()

    for i, a in enumerate(ts_svcs):
        for b in ts_svcs[i + 1:]:
            key = (min(a, b), max(a, b))
            if key in seen:
                continue
            match = _match_rule(a, b)
            if match is not None:
                weight, reason = match
                seen.add(key)
                result.append((a, b, weight, reason))

    return result


def detect_siblings_by_fuzzy(
    services: list[str],
    already_paired: set[tuple[str, str]] | None = None,
    trigram_threshold: float = 0.70,
) -> list[tuple[str, str, float, str]]:
    """Шаг 2 (P-75): нечёткий поиск сиблингов через trigram cosine.

    Trigram cosine ≥ 0.70 корректно поймает нестандартные пары (inside-payment /
    outside-payment = 0.728) и не создаст ложных срабатываний (station-food /
    train-food = 0.463, далеко ниже порога).

    Args:
        services: список имён сервисов
        already_paired: пары уже найденные rule-based — исключить их
        trigram_threshold: минимальное trigram cosine (дефолт = 0.70)

    Returns:
        Список (svc_a, svc_b, weight, reason)
    """
    if already_paired is None:
        already_paired = set()

    prefix, _ = _get_svc_naming()
    ts_svcs = [s for s in services if s.startswith(prefix)]
    result: list[tuple[str, str, float, str]] = []
    seen: set[tuple[str, str]] = set()

    for i, a in enumerate(ts_svcs):
        for b in ts_svcs[i + 1:]:
            key = (min(a, b), max(a, b))
            if key in seen or key in already_paired:
                continue
            a_s, b_s = _strip(a), _strip(b)
            if not a_s or not b_s:
                continue
            tc = _trigram_cosine(a_s, b_s)
            if tc >= trigram_threshold:
                seen.add(key)
                result.append((a, b, 0.70, f"trigram_cosine({tc:.2f})"))

    return result


def detect_all_siblings(
    services: list[str],
) -> list[tuple[str, str, float, str]]:
    """Объединяет rule-based (шаг 1) и fuzzy trigram (шаг 2).

    Returns:
        Все уникальные сиблинг-пары.
    """
    rule_pairs = detect_siblings_by_rules(services)
    ruled_keys = {(min(a, b), max(a, b)) for a, b, *_ in rule_pairs}
    fuzzy_pairs = detect_siblings_by_fuzzy(services, already_paired=ruled_keys)
    return rule_pairs + fuzzy_pairs


# ─── Вспомогательные функции для fuzzy ────────────────────────────────────────

def _trigrams(text: str) -> dict[str, int]:
    """Словарь 3-грамм с пробельным padding."""
    padded = f"  {text}  "
    result: dict[str, int] = {}
    for i in range(len(padded) - 2):
        t = padded[i: i + 3]
        result[t] = result.get(t, 0) + 1
    return result


def _trigram_cosine(a: str, b: str) -> float:
    """Cosine similarity по 3-граммам двух строк."""
    ta, tb = _trigrams(a), _trigrams(b)
    if not ta or not tb:
        return 0.0
    keys = set(ta) | set(tb)
    dot = sum(ta.get(k, 0) * tb.get(k, 0) for k in keys)
    norm_a = sum(v * v for v in ta.values()) ** 0.5
    norm_b = sum(v * v for v in tb.values()) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# ─── Правила ──────────────────────────────────────────────────────────────────

def _strip(name: str) -> str:
    """ts-travel2-service → travel2  (использует service_naming из конфига)."""
    prefix, suffix = _get_svc_naming()
    if name.startswith(prefix):
        name = name[len(prefix):]
    if name.endswith(suffix):
        name = name[: -len(suffix)]
    return name


def _match_rule(a: str, b: str) -> tuple[float, str] | None:
    checks = (
        _is_other_variant,
        _is_numeric_variant,
        _is_version_variant,
        _is_admin_variant,
        _is_func_variant,
    )
    for fn in checks:
        match = fn(a, b)
        if match is not None:
            return match
    return None


def _is_other_variant(a: str, b: str) -> tuple[float, str] | None:
    """ts-preserve-other-service ↔ ts-preserve-service."""
    a_s, b_s = _strip(a), _strip(b)
    if a_s + "-other" == b_s or b_s + "-other" == a_s:
        return SIBLING_WEIGHTS["other_variant"], "other_variant"
    return None


def _is_numeric_variant(a: str, b: str) -> tuple[float, str] | None:
    """ts-travel2-service ↔ ts-travel-service (суффикс-цифра без дефиса)."""
    a_s, b_s = _strip(a), _strip(b)
    for x, y in ((a_s, b_s), (b_s, a_s)):
        if x and x[-1].isdigit():
            stripped = x.rstrip("0123456789")
            if stripped == y:
                return SIBLING_WEIGHTS["numeric_variant"], "numeric_variant"
    return None


def _is_version_variant(a: str, b: str) -> tuple[float, str] | None:
    """ts-voucher-service-v2 ↔ ts-voucher-service (суффикс -vN после полного имени)."""
    for x, y in ((a, b), (b, a)):
        if _VERSION_RE.search(x):
            base = _VERSION_RE.sub("", x)
            if base == y:
                return SIBLING_WEIGHTS["version_variant"], "version_variant"
    return None


def _is_admin_variant(a: str, b: str) -> tuple[float, str] | None:
    """ts-admin-travel-service ↔ ts-travel-service."""
    prefix, _ = _get_svc_naming()
    admin_prefix = prefix + "admin-"
    for x, y in ((a, b), (b, a)):
        if x.startswith(admin_prefix):
            non_admin = prefix + x[len(admin_prefix):]
            if non_admin == y:
                return SIBLING_WEIGHTS["admin_variant"], "admin_variant"
    return None


def _is_func_variant(a: str, b: str) -> tuple[float, str] | None:
    """ts-route-plan-service ↔ ts-route-service (функциональный суффикс)."""
    a_s, b_s = _strip(a), _strip(b)
    func_suffixes = _get_func_suffixes()
    for x, y in ((a_s, b_s), (b_s, a_s)):
        for suffix in func_suffixes:
            if x == y + "-" + suffix:
                return SIBLING_WEIGHTS["func_variant"], "func_variant"
    return None
