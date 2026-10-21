"""Детектор SIBLING_OF рёбер по правилам именования сервисов.

Многоуровневый каскад:
  Шаг 1 (substring)    — нормализованное имя A является токеном в B: food⊂station-food  (0.60)
  Шаг 2 (rule-based)   — суффиксы (-other, числовой, -vN, admin-, func) → точные пары
  Шаг 3 (fuzzy)        — trigram cosine ≥ 0.70 — нестандартные пары (inside/outside)
  Шаг 4 (levenshtein)  — нормализованное расстояние ≤ 0.35 (пропущенные trigram-пары)

Правила (из ADR-027):
  other_variant      — ts-preserve-other-service ↔ ts-preserve-service       (0.85)
  numeric_variant    — ts-travel2-service ↔ ts-travel-service                 (0.85)
  version_variant    — ts-voucher-service-v2 ↔ ts-voucher-service             (0.85)
  admin_variant      — ts-admin-travel-service ↔ ts-travel-service            (0.60)
  func_variant       — ts-route-plan-service ↔ ts-route-service               (0.60)
  substring_variant  — ts-food-service ↔ ts-station-food-service              (0.60)

Рёбра двусторонние: добавлять a→b и b→a.
depth_cost = 0 (SIBLING_OF не расходует бюджет глубины в BFS).

prefix/suffix автовычисляются из набора сервисов через commonprefix — конфиг нужен только как fallback.
"""
from __future__ import annotations

import os
import re
from functools import lru_cache

from .._config import cfg_get

# ─── Константы ────────────────────────────────────────────────────────────────

_VERSION_RE = re.compile(r"-v\d+$")

# Веса по типу связи (фиксированные — не являются доменной конфигурацией)
SIBLING_WEIGHTS: dict[str, float] = {
    "numeric_variant":   0.85,  # travel + travel2
    "other_variant":     0.85,  # preserve + preserve-other
    "version_variant":   0.85,  # voucher + voucher-v2
    "admin_variant":     0.60,  # travel + admin-travel
    "func_variant":      0.60,  # route + route-plan, food + food-delivery
    "substring_variant": 0.60,  # food + station-food, food + train-food
    "levenshtein":       0.55,  # незначительные вариации имён
}

# ─── Ленивый кеш для service_naming и func_variant_suffixes ───────────────────
_SVC_NAMING: tuple[str, str] | None = None
_FUNC_VARIANT_SUFFIXES_CACHE: frozenset[str] | None = None


def _get_svc_naming() -> tuple[str, str]:
    """Вернуть (prefix, suffix) из конфига (fallback для _strip без списка)."""
    global _SVC_NAMING
    if _SVC_NAMING is None:
        sn = cfg_get("service_naming", {})
        _SVC_NAMING = (sn.get("prefix", "ts-"), sn.get("suffix", "-service"))
    return _SVC_NAMING


def _auto_naming(services: list[str]) -> tuple[str, str]:
    """Автовычислить общий prefix/suffix из набора имён сервисов."""
    return _auto_naming_cached(tuple(sorted(set(services))))


@lru_cache(maxsize=16)
def _auto_naming_cached(services: tuple[str, ...]) -> tuple[str, str]:
    if len(services) < 2:
        return _get_svc_naming()
    pfx = os.path.commonprefix(list(services))
    sfx = os.path.commonprefix([s[::-1] for s in services])[::-1]
    if not sfx:
        sfx = _majority_suffix(services, threshold=0.7)
    return pfx, sfx


def _majority_suffix(services: tuple[str, ...], threshold: float = 0.7) -> str:
    """Longest suffix присутствующий у ≥ threshold доли строк."""
    min_count = len(services) * threshold
    max_len = max(len(s) for s in services)
    best = ""
    for length in range(1, max_len + 1):
        counts: dict[str, int] = {}
        for s in services:
            if len(s) >= length:
                suf = s[-length:]
                counts[suf] = counts.get(suf, 0) + 1
        dominant = max(counts, key=counts.__getitem__)
        if counts[dominant] >= min_count:
            best = dominant
        else:
            break
    return best


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
    pfx: str | None = None,
    sfx: str | None = None,
) -> list[tuple[str, str, float, str]]:
    """Шаги 1-2: substring containment + rule-based суффиксы.

    pfx/sfx автовычисляются из services если не переданы.
    Returns: Список (svc_a, svc_b, weight, reason) — ненаправленные пары.
    """
    if pfx is None or sfx is None:
        pfx, sfx = _auto_naming(services)
    ts_svcs = [s for s in services if s.startswith(pfx)]
    result: list[tuple[str, str, float, str]] = []
    seen: set[tuple[str, str]] = set()

    for i, a in enumerate(ts_svcs):
        for b in ts_svcs[i + 1:]:
            key = (min(a, b), max(a, b))
            if key in seen:
                continue
            match = _match_rule(a, b, pfx, sfx)
            if match is not None:
                weight, reason = match
                seen.add(key)
                result.append((a, b, weight, reason))

    return result


def detect_siblings_by_fuzzy(
    services: list[str],
    already_paired: set[tuple[str, str]] | None = None,
    trigram_threshold: float = 0.70,
    pfx: str | None = None,
    sfx: str | None = None,
) -> list[tuple[str, str, float, str]]:
    """Шаг 3: trigram cosine ≥ 0.70.

    inside-payment / outside-payment = 0.728 (поймает).
    station-food / train-food = 0.463 (не поймает — их ловит substring).
    """
    if already_paired is None:
        already_paired = set()
    if pfx is None or sfx is None:
        pfx, sfx = _auto_naming(services)

    ts_svcs = [s for s in services if s.startswith(pfx)]
    result: list[tuple[str, str, float, str]] = []
    seen: set[tuple[str, str]] = set()

    for i, a in enumerate(ts_svcs):
        for b in ts_svcs[i + 1:]:
            key = (min(a, b), max(a, b))
            if key in seen or key in already_paired:
                continue
            a_s, b_s = _strip_ext(a, pfx, sfx), _strip_ext(b, pfx, sfx)
            if not a_s or not b_s:
                continue
            tc = _trigram_cosine(a_s, b_s)
            if tc >= trigram_threshold:
                seen.add(key)
                result.append((a, b, 0.70, f"trigram_cosine({tc:.2f})"))

    return result


def detect_siblings_by_levenshtein(
    services: list[str],
    already_paired: set[tuple[str, str]] | None = None,
    max_norm_dist: float = 0.25,
    pfx: str | None = None,
    sfx: str | None = None,
) -> list[tuple[str, str, float, str]]:
    """Шаг 4: нормализованный Левенштейн ≤ max_norm_dist.

    Ловит пары которые trigram пропустил из-за низкого token-overlap.
    norm_dist = levenshtein(a, b) / max(len(a), len(b)).
    """
    if already_paired is None:
        already_paired = set()
    if pfx is None or sfx is None:
        pfx, sfx = _auto_naming(services)

    ts_svcs = [s for s in services if s.startswith(pfx)]
    result: list[tuple[str, str, float, str]] = []
    seen: set[tuple[str, str]] = set()

    for i, a in enumerate(ts_svcs):
        for b in ts_svcs[i + 1:]:
            key = (min(a, b), max(a, b))
            if key in seen or key in already_paired:
                continue
            a_s, b_s = _strip_ext(a, pfx, sfx), _strip_ext(b, pfx, sfx)
            if not a_s or not b_s:
                continue
            dist = _levenshtein(a_s, b_s)
            norm = dist / max(len(a_s), len(b_s))
            if norm <= max_norm_dist:
                seen.add(key)
                result.append((a, b, SIBLING_WEIGHTS["levenshtein"], f"levenshtein({dist},{norm:.2f})"))

    return result


def detect_all_siblings(
    services: list[str],
) -> list[tuple[str, str, float, str]]:
    """Каскад: substring+rules → trigram → levenshtein.

    Returns: все уникальные сиблинг-пары (наиболее точные первыми).
    """
    pfx, sfx = _auto_naming(services)

    rule_pairs = detect_siblings_by_rules(services, pfx=pfx, sfx=sfx)
    ruled_keys = {(min(a, b), max(a, b)) for a, b, *_ in rule_pairs}

    fuzzy_pairs = detect_siblings_by_fuzzy(services, already_paired=ruled_keys, pfx=pfx, sfx=sfx)
    fuzzy_keys = ruled_keys | {(min(a, b), max(a, b)) for a, b, *_ in fuzzy_pairs}

    lev_pairs = detect_siblings_by_levenshtein(services, already_paired=fuzzy_keys, pfx=pfx, sfx=sfx)

    return rule_pairs + fuzzy_pairs + lev_pairs


# ─── Вспомогательные функции ──────────────────────────────────────────────────

def _strip(name: str) -> str:
    """ts-travel2-service → travel2  (использует service_naming из конфига — fallback)."""
    prefix, suffix = _get_svc_naming()
    return _strip_ext(name, prefix, suffix)


@lru_cache(maxsize=2048)
def _strip_ext(name: str, pfx: str, sfx: str) -> str:
    """ts-travel2-service → travel2 с явными prefix/suffix. LRU-кешируется."""
    if pfx and name.startswith(pfx):
        name = name[len(pfx):]
    if sfx and name.endswith(sfx):
        name = name[: -len(sfx)]
    return name


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


def _levenshtein(a: str, b: str) -> int:
    """Расстояние Левенштейна между строками a и b."""
    m, n = len(a), len(b)
    dp = list(range(n + 1))
    for i in range(1, m + 1):
        prev, dp[0] = dp[0], i
        for j in range(1, n + 1):
            temp = dp[j]
            dp[j] = prev if a[i - 1] == b[j - 1] else 1 + min(dp[j], dp[j - 1], prev)
            prev = temp
    return dp[n]


# ─── Правила ──────────────────────────────────────────────────────────────────

def _match_rule(a: str, b: str, pfx: str = "", sfx: str = "") -> tuple[float, str] | None:
    if not pfx and not sfx:
        pfx, sfx = _get_svc_naming()
    # Порядок важен: точные паттерны (-other, числовой, -vN, admin-) идут до substring,
    # чтобы возвращать правильный reason для классификатора G-side.
    checks = (
        lambda x, y: _is_other_variant(x, y, pfx, sfx),
        lambda x, y: _is_numeric_variant(x, y, pfx, sfx),
        _is_version_variant,
        lambda x, y: _is_admin_variant(x, y, pfx),
        lambda x, y: _is_func_variant(x, y, pfx, sfx),
        lambda x, y: _is_substring_variant(x, y, pfx, sfx),
    )
    for fn in checks:
        match = fn(a, b)
        if match is not None:
            return match
    return None


def _is_substring_variant(a: str, b: str, pfx: str, sfx: str) -> tuple[float, str] | None:
    """Нормализованное имя A является целым токеном (через '-') в нормализованном B."""
    a_s, b_s = _strip_ext(a, pfx, sfx), _strip_ext(b, pfx, sfx)
    for shorter, longer in ((a_s, b_s), (b_s, a_s)):
        if not shorter or not longer or len(shorter) >= len(longer):
            continue
        i = longer.find(shorter)
        while i != -1:
            before_ok = (i == 0 or longer[i - 1] == "-")
            after_ok = (i + len(shorter) == len(longer) or longer[i + len(shorter)] == "-")
            if before_ok and after_ok:
                return SIBLING_WEIGHTS["substring_variant"], f"substring({shorter}⊂{longer})"
            i = longer.find(shorter, i + 1)
    return None


def _is_other_variant(a: str, b: str, pfx: str, sfx: str) -> tuple[float, str] | None:
    """ts-preserve-other-service ↔ ts-preserve-service."""
    a_s, b_s = _strip_ext(a, pfx, sfx), _strip_ext(b, pfx, sfx)
    if a_s + "-other" == b_s or b_s + "-other" == a_s:
        return SIBLING_WEIGHTS["other_variant"], "other_variant"
    return None


def _is_numeric_variant(a: str, b: str, pfx: str, sfx: str) -> tuple[float, str] | None:
    """ts-travel2-service ↔ ts-travel-service (суффикс-цифра без дефиса)."""
    a_s, b_s = _strip_ext(a, pfx, sfx), _strip_ext(b, pfx, sfx)
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


def _is_admin_variant(a: str, b: str, pfx: str) -> tuple[float, str] | None:
    """ts-admin-travel-service ↔ ts-travel-service."""
    admin_prefix = pfx + "admin-"
    for x, y in ((a, b), (b, a)):
        if x.startswith(admin_prefix):
            non_admin = pfx + x[len(admin_prefix):]
            if non_admin == y:
                return SIBLING_WEIGHTS["admin_variant"], "admin_variant"
    return None


def _is_func_variant(a: str, b: str, pfx: str, sfx: str) -> tuple[float, str] | None:
    """ts-route-plan-service ↔ ts-route-service (функциональный суффикс)."""
    a_s, b_s = _strip_ext(a, pfx, sfx), _strip_ext(b, pfx, sfx)
    func_suffixes = _get_func_suffixes()
    for x, y in ((a_s, b_s), (b_s, a_s)):
        for suffix in func_suffixes:
            if x == y + "-" + suffix:
                return SIBLING_WEIGHTS["func_variant"], "func_variant"
    return None
