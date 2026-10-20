"""FSM Transitions — расширение hs через топологические переходы.

Два режима использования:

1. forward_expand(hs, ix):
   Если entry handler уже в hs → добавить его downstream handlers (conservative).

2. reverse_expand(hs, ranked, ix, top_k=50):
   Если downstream handler попал в top-K ranked → добавить entry handler в hs.
   Затем BFS closure подхватит весь chain от entry.

Transitions строятся offline скриптом scripts/gen_fsm_transitions.py
из топологии скелета (calls + overrides + remote_call) — без хардкода.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.core.skeleton_index import Index

_FORWARD: dict[str, list[dict]] = {}
_REVERSE: dict[str, list[str]] = {}
_LOADED = False

# Кеш предвычисленных BGE-M3 векторов для condition_text.
# Ключ: condition_text строка → нормализованный ndarray.
# Заполняется лениво при первом вызове forward_expand с encoder.
_COND_VECS: "dict[str, object]" = {}
_COND_VECS_READY = False

# Cosine similarity threshold для активации conditional transition.
# 0.35: "order includes food" vs "Book a ticket with food" → ~0.55 ✓
#       "order includes food" vs "Book a regular ticket"   → ~0.20 ✗
VECTOR_THRESHOLD = 0.35


def load_fsm_transitions(cache_path: str | Path | None = None) -> bool:
    """Загрузить transitions из cache/fsm_transitions.json.

    Возвращает True если загрузка прошла успешно.
    """
    global _FORWARD, _REVERSE, _LOADED
    if _LOADED:
        return True

    if cache_path is None:
        # Поиск относительно этого файла
        here = Path(__file__).parent
        for candidate in [
            here.parent.parent / "cache" / "fsm_transitions.json",
            Path("cache/fsm_transitions.json"),
        ]:
            if candidate.exists():
                cache_path = candidate
                break

    if not cache_path or not Path(cache_path).exists():
        return False

    try:
        with open(cache_path) as f:
            data = json.load(f)
        _FORWARD = data.get("forward", {})
        _REVERSE = data.get("reverse", {})
        _LOADED = True
        print(
            f"[fsm] forward={len(_FORWARD)} entries, reverse={len(_REVERSE)} entries",
            file=sys.stderr,
        )
        return True
    except Exception as e:
        print(f"[fsm] Ошибка загрузки transitions: {e}", file=sys.stderr)
        return False


def _precompute_cond_vecs(encoder) -> None:
    """Предвычислить BGE-M3 векторы для всех уникальных condition_text."""
    global _COND_VECS, _COND_VECS_READY
    if _COND_VECS_READY:
        return
    _COND_VECS_READY = True  # установить до encode чтобы не зациклиться
    if not encoder or not encoder.enabled:
        return
    texts: list[str] = []
    for transitions in _FORWARD.values():
        for t in transitions:
            ct = (t.get("condition") or {}).get("condition_text", "")
            if ct and ct not in _COND_VECS:
                texts.append(ct)
    if not texts:
        return
    vecs = encoder.encode_batch(texts)
    if vecs is None:
        return
    for text, vec in zip(texts, vecs):
        _COND_VECS[text] = vec
    print(f"[fsm] condition vectors: {len(_COND_VECS)} unique texts encoded", file=__import__("sys").stderr)


CE_THRESHOLD = 0.85  # CrossEncoder sigmoid > 0.85 → condition active
# 0.85 chosen to exclude boundary FP cases:
#   "payment successful" for external-pay step = 0.5053 → skip ✓
#   "order status not suitable" for refused-rebook = 0.8285 → skip ✓
# Decomposed service queries bypass raw-text low-score issue:
#   raw "Book with food from station" → "order includes food" = 0.22 ✗
#   decomposed "food station" → "order includes food" = 0.976 ✓


_FSM_STEP_TYPES = frozenset({"book", "rebook"})


def _service_queries(step_params: "object | None") -> list[str]:
    """Извлечь natural language condition queries из StepParams.services (Qwen output).

    ServiceItem(present=True) → query для CE matching против FSM condition_text.
    Трансформация: name.replace('_',' ').replace('/',' ')

    Активируется только для book/rebook шагов: FSM food/assurance/seat conditions
    релевантны только при бронировании, а не при browse/search/cancel.

    Примеры (verified CE scores >= 0.85):
      food_station → "food station" vs "order includes food" = 0.976
      food_train   → "food train" vs "order includes food" = 0.989
      insurance    → "insurance" vs "order requires assurance" = 0.945
      assurance    → "assurance" vs "order requires assurance" = 0.974
    """
    if step_params is None:
        return []
    step_type = getattr(step_params, "step_type", None)
    if step_type not in _FSM_STEP_TYPES:
        return []
    result: list[str] = []
    for svc in getattr(step_params, "services", []):
        if getattr(svc, "present", False):
            query = getattr(svc, "name", "").replace("_", " ").replace("/", " ").strip()
            if query:
                result.append(query)
    return result


def _condition_matches(
    condition: "dict | None",
    action_text: "str | None",
    action_vec,
    encoder,
    cross_encoder=None,
    service_queries: "list[str] | None" = None,
) -> bool:
    """Проверить совпадение condition через CrossEncoder (если доступен) или BGE-M3.

    Приоритет:
    0. Decomposed service queries (от Qwen StepParams.services):
       - каждый query = natural language описание одного сервиса
       - CE(query, condition_text) >= CE_THRESHOLD → ACTIVE
       - CE scores: "food station" vs "order includes food" = 0.976 ✓
       - negation: absent service → не в queries → condition не активируется ✓
    1. CrossEncoder на raw action_text (fallback если service_queries пустые)
    2. BGE-M3 cosine fallback

    Unconditional transitions → пропускаем (источник FP от cancel/execute → order-other).
    """
    if condition is None:
        return False
    if not condition.get("is_conditional", False):
        return False  # unconditional → skip
    condition_text = condition.get("condition_text", "")
    if not condition_text:
        return False

    # Путь 0: Decomposed service queries от Qwen (semantic bridge NLP → FSM)
    if cross_encoder is not None and cross_encoder.enabled and service_queries:
        for query in service_queries:
            score = cross_encoder.score_pair(query, condition_text)
            if score >= CE_THRESHOLD:
                return True
        # service_queries непустые но не matched → condition не активна
        # (explicit negation handled: absent service → not in service_queries)
        return False

    # Путь 1: CrossEncoder на raw action_text
    if cross_encoder is not None and cross_encoder.enabled and action_text:
        score = cross_encoder.score_pair(action_text, condition_text)
        return score >= CE_THRESHOLD

    # Путь 2: BGE-M3 cosine (fallback)
    if action_vec is None or not encoder or not encoder.enabled:
        return False
    cond_vec = _COND_VECS.get(condition_text)
    if cond_vec is None:
        return False
    import numpy as np
    similarity = float(np.dot(action_vec, cond_vec))
    return similarity >= VECTOR_THRESHOLD


def forward_expand(
    hs: set[str],
    ix: "Index",
    action_text: str | None = None,
    encoder=None,
    cross_encoder=None,
    step_params: "object | None" = None,
    max_chain: int = 20,
) -> set[str]:
    """Добавить downstream handlers через векторное matching condition_text.

    Алгоритм:
    1. Если CrossEncoder доступен — использовать score_pair(action_text, condition_text).
       CrossEncoder понимает negation ("without food" → condition False).
    2. Иначе: предвычислить BGE-M3 векторы для condition_text + cosine similarity.
    3. Conditional transitions only (unconditional — источник FP).

    max_chain: пропускать entries с >max_chain transitions.
    """
    if not _LOADED or not _FORWARD:
        return hs

    # BGE-M3 предвычисление нужно только для fallback (без CrossEncoder)
    if cross_encoder is None or not cross_encoder.enabled:
        _precompute_cond_vecs(encoder)

    # Закодировать текст шага (только для BGE-M3 fallback)
    action_vec = None
    if (cross_encoder is None or not cross_encoder.enabled) and encoder and encoder.enabled and action_text:
        action_vec = encoder.encode_single(action_text)

    # Decomposed condition queries от Qwen StepParams.services (semantic bridge)
    svc_queries = _service_queries(step_params)

    added: list[str] = []
    new_hs = set(hs)
    hs_mods = {ix.module.get(h, "") for h in hs}

    for h in list(hs):
        transitions = _FORWARD.get(h, [])
        if len(transitions) > max_chain:
            continue
        for t in transitions:
            to_nid = t["to_nid"]
            to_mod = t["to_module"]
            if to_nid in new_hs:
                continue
            condition = t.get("condition")
            if not _condition_matches(condition, action_text, action_vec, encoder, cross_encoder,
                                      service_queries=svc_queries):
                continue
            new_hs.add(to_nid)
            if to_mod not in hs_mods:
                hs_mods.add(to_mod)
                added.append(f"{to_mod}::{t['to_handler']}")

    if added:
        print(
            f"[fsm] forward_expand: +{len(added)} handlers: {added[:5]}",
            file=sys.stderr,
        )
    return new_hs


def reverse_expand(
    hs: set[str],
    ranked: list[tuple[float, str]],
    ix: "Index",
    top_k: int = 50,
) -> set[str]:
    """Добавить entry handlers если их downstream попал в top-K ranked.

    Если BM25/BGE-M3 нашли downstream handler (например order::createNewOrder)
    → добавить entry handler (preserve::preserve) в hs → BFS closure развернёт chain.

    Фильтр безопасности: добавляем entry только если entry сам тоже присутствует
    в ranked на любой позиции. Если BM25 вообще не нашёл preserve → не добавляем.
    """
    if not _LOADED or not _REVERSE:
        return hs

    # Все nid из ranked (любая позиция) — для фильтрации
    all_ranked_nids = {nid for _, nid in ranked}
    # Топ-K downstream кандидатов
    top_nids = {nid for _, nid in ranked[:top_k]}

    added: list[str] = []
    new_hs = set(hs)
    hs_mods = {ix.module.get(h, "") for h in hs}

    for to_nid in top_nids:
        entry_nids = _REVERSE.get(to_nid, [])
        for entry_nid in entry_nids:
            if entry_nid in new_hs:
                continue
            # Фильтр: entry handler должен быть в ranked (BM25 его хоть как-то нашёл)
            if entry_nid not in all_ranked_nids:
                continue
            entry_mod = ix.module.get(entry_nid, "")
            entry_name = ix.N.get(entry_nid, {}).get("name", "?")
            new_hs.add(entry_nid)
            if entry_mod not in hs_mods:
                hs_mods.add(entry_mod)
                added.append(f"{entry_mod}::{entry_name}")

    if added:
        print(
            f"[fsm] reverse_expand: +{len(added)} entries: {added[:5]}",
            file=sys.stderr,
        )
    return new_hs
