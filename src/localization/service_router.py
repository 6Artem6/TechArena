"""Service Router — FSM перевод шагов на язык сервисных описаний.

Для каждого шага сценария определяет явно какие сервисы задействованы.
Это дополняет BM25 routing там где proximity-matching ошибается:
  - "Try to cancel (system refuses)" → ts-cancel-service (failed op всё равно вызывает)
  - "Book regular train" → ts-preserve-other-service (не preserve-service!)
  - Шаги со слабым vocabulary overlap (rebook, consign, execute)

Метод: BGE-M3 similarity(action) vs service profile descriptions → top-K services.
Кешируется по (action, train_type) в памяти процесса.

Zero-hardcode: сервисные описания берутся из service_profiles_v1.pkl,
которые выводятся из кода через gen_service_profiles.py.
"""
from __future__ import annotations

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.core.skeleton_index import Index


_ROUTER_CACHE: dict[tuple, list[str]] = {}
_SVC_VECS: dict[str, object] | None = None  # module → np.ndarray
_PROFILE_PHRASES: dict[str, list[str]] | None = None

# Минимальный порог cosine similarity для включения сервиса
_SVC_SCORE_THRESHOLD = 0.55
# Top-K сервисов берём из router
_SVC_TOP_K = 3


def _init_router(encoder, profiles: dict) -> None:
    """Предвычислить векторы для service profile descriptions.

    Вызывать из searcher._init_service_matcher() после инициализации encoder.
    """
    global _SVC_VECS, _PROFILE_PHRASES
    if _SVC_VECS is not None:
        return
    if not encoder.enabled:
        _SVC_VECS = {}
        return

    import numpy as np

    vecs: dict[str, object] = {}
    phrases_map: dict[str, list[str]] = {}

    for mod, prof in profiles.items():
        phrases = getattr(prof, "vector_search_phrases", [])[:4]
        if not phrases:
            phrases = [" ".join(getattr(prof, "bm25_keywords", [])[:6])]
        phrases = [p for p in phrases if p.strip()]
        if not phrases:
            continue
        phrases_map[mod] = phrases
        # Encode: среднее по всем фразам
        vecs_list = encoder._model.encode(
            phrases,
            normalize_embeddings=True,
            batch_size=32,
            show_progress_bar=False,
        )
        vecs[mod] = np.array(vecs_list).mean(axis=0)

    # Нормализовать средние векторы
    for mod in list(vecs.keys()):
        v = vecs[mod]
        norm = float(np.linalg.norm(v))
        if norm > 1e-9:
            vecs[mod] = v / norm
        else:
            del vecs[mod]

    _SVC_VECS = vecs
    _PROFILE_PHRASES = phrases_map
    print(
        f"[service_router] {len(_SVC_VECS)} service vectors precomputed",
        file=sys.stderr,
    )


def route_action_to_services(
    action: str,
    train_type: str,
    encoder,
) -> list[str]:
    """Вернуть top-K module names для данного action.

    Использует BGE-M3 cosine similarity(action, service description).
    Возвращает только сервисы с score > _SVC_SCORE_THRESHOLD.
    """
    global _ROUTER_CACHE, _SVC_VECS

    if not _SVC_VECS:
        return []

    cache_key = (action, train_type)
    if cache_key in _ROUTER_CACHE:
        return _ROUTER_CACHE[cache_key]

    import numpy as np

    act_vec = encoder._model.encode(
        action,
        normalize_embeddings=True,
        show_progress_bar=False,
    )

    scores: list[tuple[float, str]] = []
    for mod, svc_vec in _SVC_VECS.items():
        score = float(np.dot(act_vec, svc_vec))
        scores.append((score, mod))

    scores.sort(reverse=True)
    result = [
        mod
        for sc, mod in scores[:_SVC_TOP_K]
        if sc >= _SVC_SCORE_THRESHOLD
    ]

    _ROUTER_CACHE[cache_key] = result
    return result


def add_service_handlers(
    hs: set[str],
    action: str,
    train_type: str,
    encoder,
    ix: "Index",
    ranked: list[tuple[float, str]],
) -> set[str]:
    """Добавить в hs handlers из top-K сервисов найденных service router.

    Добавляет handler только если сервис НЕ представлен в hs.
    Берёт best-ranked handler из сервиса (по BM25/BGE-M3 score).

    Возвращает расширенный hs (новый set).
    """
    if not _SVC_VECS:
        return hs

    services = route_action_to_services(action, train_type, encoder)
    if not services:
        return hs

    # Сервисы уже представленные в hs
    hs_mods: set[str] = {ix.module.get(h, "") for h in hs}

    new_hs = set(hs)
    added: list[str] = []

    for svc_mod in services:
        if svc_mod in hs_mods:
            continue  # сервис уже в hs
        # Найти best ranked handler из этого сервиса
        for _, h in ranked:
            if ix.module.get(h) == svc_mod:
                new_hs.add(h)
                hs_mods.add(svc_mod)
                added.append(svc_mod)
                break
        # Если не найден в ranked — fallback к первому non-trivial handler
        else:
            for nid, _verb, _tpl in ix.handlers:
                if ix.module.get(nid) != svc_mod:
                    continue
                name = ix.N.get(nid, {}).get("name", "")
                if name in ("home", "welcome", "test", "index"):
                    continue
                new_hs.add(nid)
                hs_mods.add(svc_mod)
                added.append(svc_mod)
                break

    return new_hs
