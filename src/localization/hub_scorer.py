"""Hub-based entry point scoring using call graph structure.

Идея: genuine entry point для multi-service операции является
топологическим хабом — вызывает другие handlers, которые
тоже релевантны для того же запроса.

Пример:
  preserve.preserve() → вызывает travel-service, order-service,
                         seat-service, food-service, execute-service
  Для запроса "book train ticket" все эти сервисы в BM25 top-N.
  → preserve.preserve получает высокий hub score.

  cancel.calculate() → вызывает только order-service, refund-service.
  Для запроса "search trains" эти сервисы НЕ в top-N.
  → cancel.calculate получает низкий hub score.

Алгоритм (без domain knowledge):
  1. precompute_reachability(ix) → {handler → frozenset[reachable_handlers]}
     BFS по calls + remote_call + over edges.
  2. hub_score(h, scored) = Σ score(h2) for h2 in reachable[h] ∩ scored
  3. Финальный score = (1 - gamma) * current + gamma * hub_score_norm
"""
from __future__ import annotations

import collections
import logging

_log = logging.getLogger(__name__)

# Кеш: сохраняется между вызовами enhanced_ground для одного index
_REACHABILITY: "dict[str, frozenset[str]] | None" = None


def precompute_reachability(ix) -> dict[str, frozenset[str]]:
    """Предвычислить множества достижимых handlers для каждого handler.

    Использует BFS по call + remote_call + over (интерфейс → реализация).
    Сохраняет результат в глобальный кеш — вычисляется один раз.
    """
    global _REACHABILITY
    if _REACHABILITY is not None:
        return _REACHABILITY

    handler_set = frozenset(h for h, _, _ in ix.handlers)

    # Построить смежность method → {reachable_methods}
    adj: dict[str, set[str]] = collections.defaultdict(set)

    for src, targets in ix.calls.items():
        adj[src].update(targets)

    for src, targets in ix.wiring.get("remote_call", {}).items():
        adj[src].update(targets)

    # Интерфейс → реализации (over): если вызван интерфейсный метод, достижимы его реализации
    for target, impls in ix.over.items():
        adj[target].update(impls)

    # BFS для каждого handler (ищем только другие handlers как цели)
    result: dict[str, frozenset[str]] = {}
    for h, _, _ in ix.handlers:
        visited: set[str] = set()
        queue: list[str] = [h]
        while queue:
            curr = queue.pop()
            if curr in visited:
                continue
            visited.add(curr)
            for nxt in adj.get(curr, ()):
                if nxt not in visited:
                    queue.append(nxt)
        result[h] = frozenset(visited & handler_set) - {h}

    _log.info(
        "hub_scorer: предвычислено достижимость для %d handlers, "
        "средний размер %.1f",
        len(result),
        sum(len(v) for v in result.values()) / max(len(result), 1),
    )
    _REACHABILITY = result
    return result


def rerank_by_hub(
    candidates: list[tuple[float, str]],
    reachable: dict[str, frozenset[str]],
    gamma: float = 0.25,
    top_k: int = 30,
) -> list[tuple[float, str]]:
    """Переранжировать кандидатов с учётом hub score.

    hub_score(h) = сумма текущих оценок handlers, достижимых из h,
                   среди top_k кандидатов.

    gamma=0 → без изменений. gamma=0.25 → 25% hub score.
    """
    if not candidates or gamma == 0.0:
        return candidates

    # Рабочее множество: top_k кандидатов
    work = candidates[:top_k]
    score_map = {h: sc for sc, h in work}

    # Hub score: сумма нормированных оценок достижимых handlers
    max_sc = max(score_map.values()) or 1.0
    norm_map = {h: sc / max_sc for h, sc in score_map.items()}

    hub: dict[str, float] = {}
    for sc, h in work:
        reachable_in_top = reachable.get(h, frozenset()) & score_map.keys()
        hub[h] = sum(norm_map.get(h2, 0.0) for h2 in reachable_in_top)

    if not any(hub.values()):
        return candidates

    hub_max = max(hub.values()) or 1.0

    # Аддитивный boost: сохраняем исходный масштаб scores (важно для tau-window).
    # new_score = sc + gamma * (hub_norm / hub_max) * max_sc
    # → boost пропорционален исходному best score → tau-window не ломается.
    combined: list[tuple[float, str]] = [
        (
            sc + gamma * (hub.get(h, 0.0) / hub_max) * max_sc,
            h,
        )
        for sc, h in work
    ]
    # Handlers за пределами top_k оставить в конце без изменений
    tail = [(sc, h) for sc, h in candidates[top_k:] if h not in score_map]

    return sorted(combined, reverse=True) + tail
