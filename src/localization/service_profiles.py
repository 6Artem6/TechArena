"""Service Profile — автоматически генерируемые профили сервисов (P-101).

Заменяет ручные словари (step_rules, tier4_remote_tokens, func_variant_suffixes):
  - one_word_tag        — семантическая метка для быстрой фильтрации
  - bm25_keywords       — BM25-термины из живого кода
  - vector_search_phrases — NL-фразы для сервисного intent-matching
  - handled_parameters  — какие booking-параметры обрабатывает сервис

Оффлайн: gen_service_profiles.py → cache/service_profiles_v2.pkl
Runtime: load_profiles() → score_profiles(action) → top-K сервисов
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

_CACHE_KEY_VERSION = "v2"


# ---------------------------------------------------------------------------
# Dataclass профиля
# ---------------------------------------------------------------------------

@dataclass
class ServiceProfile:
    service_name: str                           # "ts-preserve-service"
    one_word_tag: str                           # "preserve"
    bm25_keywords: list[str] = field(default_factory=list)
    vector_search_phrases: list[str] = field(default_factory=list)
    handled_parameters: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)  # semantic tags (Qwen 1.5b offline)


# ---------------------------------------------------------------------------
# Загрузка из кеша
# ---------------------------------------------------------------------------

_profiles_cache: dict[str, ServiceProfile] | None = None


def load_profiles(cache_path: str | None = None) -> dict[str, ServiceProfile]:
    """Загрузить профили из кеша. Ключ = service_name."""
    global _profiles_cache
    if _profiles_cache is not None:
        return _profiles_cache

    import pickle
    from pathlib import Path

    if cache_path is None:
        from src._config import cfg_cache_dir
        cache_path = str(cfg_cache_dir() / f"service_profiles_{_CACHE_KEY_VERSION}.pkl")

    path = Path(cache_path)
    if not path.exists():
        _profiles_cache = {}
        return _profiles_cache

    with open(path, "rb") as f:
        raw = pickle.load(f)

    _profiles_cache = {}
    for item in raw.values():
        if isinstance(item, ServiceProfile):
            _profiles_cache[item.service_name] = item

    return _profiles_cache


def invalidate_cache() -> None:
    global _profiles_cache
    _profiles_cache = None


# ---------------------------------------------------------------------------
# IDF по корпусу всех профилей (BM25 для _disambiguate_func_variant)
# ---------------------------------------------------------------------------

_idf_cache: dict[str, float] | None = None


def compute_profile_idf(profiles: "dict[str, ServiceProfile] | None" = None) -> dict[str, float]:
    """IDF для каждого токена из (vector_search_phrases + bm25_keywords) всех профилей.

    Используется для взвешенного сравнения сервисов: редкие термины (cheapest,
    transfer, fewest) имеют высокий IDF; частые (trips, route) — низкий.
    Вычисляется один раз и кешируется.
    """
    global _idf_cache
    if _idf_cache is not None:
        return _idf_cache

    from math import log
    if profiles is None:
        profiles = load_profiles()

    N = len(profiles)
    if N == 0:
        _idf_cache = {}
        return _idf_cache

    df: dict[str, int] = {}
    for prof in profiles.values():
        doc_terms = set(re.findall(
            r"[a-z]+",
            " ".join(prof.vector_search_phrases) + " " + " ".join(prof.bm25_keywords),
        ))
        for t in doc_terms:
            df[t] = df.get(t, 0) + 1

    # BM25 IDF: log((N - df + 0.5) / (df + 0.5) + 1), всегда ≥ 0
    _idf_cache = {
        t: log((N - n + 0.5) / (n + 0.5) + 1)
        for t, n in df.items()
    }
    return _idf_cache


# ---------------------------------------------------------------------------
# Runtime: BM25-скоринг action_text vs vector_search_phrases
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z]+", text.lower())


def score_profiles(
    action_text: str,
    profiles: dict[str, ServiceProfile],
    top_k: int = 5,
) -> list[tuple[str, float]]:
    """Вернуть top-K сервисов по BM25 (action_text vs vector_search_phrases + bm25_keywords).

    Returns: список (service_name, score) sorted by score desc.
    """
    if not profiles:
        return []

    query_toks = _tokenize(action_text)
    if not query_toks:
        return []

    query_freq: dict[str, int] = {}
    for t in query_toks:
        query_freq[t] = query_freq.get(t, 0) + 1

    scores: list[tuple[str, float]] = []
    for svc_name, prof in profiles.items():
        # Документ = все phrases + keywords
        doc_words = _tokenize(
            " ".join(prof.vector_search_phrases) + " " + " ".join(prof.bm25_keywords)
        )
        doc_freq: dict[str, int] = {}
        for w in doc_words:
            doc_freq[w] = doc_freq.get(w, 0) + 1

        # Simple TF overlap score
        score = sum(query_freq.get(w, 0) * cnt for w, cnt in doc_freq.items())
        if score > 0:
            scores.append((svc_name, float(score)))

    scores.sort(key=lambda x: -x[1])
    return scores[:top_k]


def get_bm25_extra_tokens(
    action_text: str,
    profiles: dict[str, ServiceProfile],
    top_k: int = 3,
) -> list[str]:
    """Получить extra BM25-токены из top-K сервисных профилей для augmentation запроса."""
    top = score_profiles(action_text, profiles, top_k=top_k)
    extra: list[str] = []
    seen: set[str] = set(_tokenize(action_text))
    for svc_name, _ in top:
        prof = profiles[svc_name]
        for kw in prof.bm25_keywords:
            for tok in _tokenize(kw):
                if tok not in seen and len(tok) > 2:
                    extra.append(tok)
                    seen.add(tok)
    return extra
