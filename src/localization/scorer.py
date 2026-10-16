"""Scorer: нормированный BM25 по документу кода и документу UI.

Замена baseline_nlp.Scorer — без зависимости от baseline-скриптов.

Алгоритм ранжирования:
  1. BM25 по code-документам → нормировать на max балл запроса
  2. BM25 по ui-документам   → нормировать на max балл запроса
  3. Суммировать нормированные баллы
  → сортировать по убыванию суммы
"""
from __future__ import annotations

from ..core.bm25 import BM25


class Scorer:
    """Нормированная сумма BM25(code) + BM25(ui) для ранжирования обработчиков.

    Использует src.core.bm25.BM25 — независимая реализация.
    """

    def __init__(
        self,
        code: dict[str, list[str]],
        ui: dict[str, list[str]],
    ) -> None:
        self._bm_code = BM25(code)
        self._bm_ui = BM25(ui)

    def rank(self, q: list[str]) -> list[tuple[float, str]]:
        """Ранжировать обработчики по запросу.

        → [(score, handler_id)] по убыванию, score ∈ [0, 2].
        """
        parts: list[dict[str, float]] = []
        for bm in (self._bm_code, self._bm_ui):
            r = bm.rank(q)
            m = r[0][0] if r else 1.0
            parts.append({h: sc / m for sc, h in r})

        sc: dict[str, float] = {
            h: parts[0].get(h, 0.0) + parts[1].get(h, 0.0)
            for h in set(parts[0]) | set(parts[1])
        }
        return sorted(((v, h) for h, v in sc.items()), reverse=True)
