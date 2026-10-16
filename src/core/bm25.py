"""BM25Okapi — единая реализация для всего проекта.

Используется в:
  - src/localization/scorer.py   (поиск entry-point handlers)
  - src/localization/service_matcher.py  (ServiceMatcher: service-level BM25)
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Sequence


class BM25:
    """BM25Okapi по словарям документов.

    Принимает docs: dict[id, list[str]] — мешки слов.
    Возвращает ранжированный список через rank().
    """

    def __init__(
        self,
        docs: dict,
        k1: float = 1.2,
        b: float = 0.75,
    ) -> None:
        self.ids: list = list(docs)
        D = [docs[i] for i in self.ids]
        self.tf: list[Counter] = [Counter(d) for d in D]
        self.lens: list[int] = [len(d) for d in D]
        self.avg: float = sum(self.lens) / max(1, len(D))
        self.k1, self.b = k1, b
        df = Counter(t for c in self.tf for t in c)
        self.idf: dict[str, float] = {
            t: math.log(1 + (len(D) - n + 0.5) / (n + 0.5))
            for t, n in df.items()
        }

    def rank(self, q: Sequence[str]) -> list[tuple[float, object]]:
        """Ранжировать документы по запросу.

        Возвращает [(score, doc_id)] по убыванию, только ненулевые.
        """
        qc = Counter(q)
        sc: list[tuple[float, object]] = []
        for i, tf in enumerate(self.tf):
            s = 0.0
            for t, qn in qc.items():
                if t in tf:
                    n = tf[t]
                    tf_norm = n * (self.k1 + 1) / (
                        n + self.k1 * (1 - self.b + self.b * self.lens[i] / self.avg)
                    )
                    s += self.idf[t] * tf_norm * min(qn, 3)
            if s > 0:
                sc.append((s, self.ids[i]))
        return sorted(sc, reverse=True)
