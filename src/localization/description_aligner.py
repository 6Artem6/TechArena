"""Description-to-Step Alignment.

Алайнирует предложения из scenario.description к конкретному шагу (step N).
Позволяет извлечь из description дополнительный контекст недоступный в кратком action-тексте:
  - тип food (on train vs station store)
  - тип assurance (first type / second type)
  - вес консигнации
  - анафорные ссылки ("the first contact", "that trip")

Механизм: token overlap (Jaccard) — достаточен т.к. description ≈ пересказ steps.
Fallback BGE-M3 не нужен: лексическое перекрытие высокое по конструкции.
"""
from __future__ import annotations

import re

# step_types для которых title-токены полезны в BM25-запросе
_TITLE_INJECT_STEP_TYPES = frozenset({
    "book", "pay", "rebook", "view_trip",
    "query_insurance", "add_consign", "query_consign",
    "collect", "enter_station",
})

# Стоп-слова для title-токенов: общие + search-criteria (вредны в book/pay контексте)
_STOP = frozenset({
    "a", "an", "the", "on", "in", "at", "of", "for", "and", "or",
    "with", "from", "to", "by", "is", "are", "was", "be", "been",
    "this", "that", "it", "its", "their", "they", "them",
    "have", "has", "will", "can", "do", "did", "not", "no",
    # search-criteria: не нужны в booking-шагах
    "cheapest", "quickest", "fewest", "stops", "trips", "advanced",
    "search", "transfer", "buying", "ticket", "tickets",
    "paying", "collecting", "boarding", "account", "registered",
})


def _tokenize(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", text.lower()) if len(w) > 2}


def _split_sentences(text: str) -> list[str]:
    """Разбить description на смысловые фразы по '. ' и '; '."""
    if not text:
        return []
    # Сначала по '. ' (sentence boundary)
    parts = re.split(r"\.\s+|\.\Z", text)
    result = []
    for part in parts:
        part = part.strip()
        if len(part) < 12:
            continue
        # Длинные предложения дополнительно разбиваем по "; " и " and they"
        sub = re.split(r";\s+", part)
        result.extend(s.strip() for s in sub if len(s.strip()) > 8)
    return result


def _overlap(step_toks: set[str], sent: str) -> float:
    sent_toks = _tokenize(sent)
    inter = len(step_toks & sent_toks)
    union = len(step_toks | sent_toks)
    return inter / (union + 1e-9) if union else 0.0


class DescriptionAligner:
    """Выравниватель description-предложений к шагам сценария."""

    def __init__(self, description: str, title: str = ""):
        self._sents: list[str] = _split_sentences(description)
        self._title_toks: list[str] = [
            w for w in re.findall(r"[a-z]+", title.lower())
            if w not in _STOP and len(w) > 3
        ]

    def align(self, step_text: str, min_score: float = 0.25) -> str | None:
        """Вернуть наиболее релевантное предложение description для данного шага.

        Returns None если similarity < min_score (шаг не представлен в description).
        """
        if not self._sents:
            return None
        step_toks = _tokenize(step_text)
        best_sent, best_sc = None, min_score
        for sent in self._sents:
            sc = _overlap(step_toks, sent)
            if sc > best_sc:
                best_sent, best_sc = sent, sc
        return best_sent

    def title_tokens(self, step_type: str) -> list[str]:
        """BM25-токены из title для данного step_type.

        Инжектируются только для типов шагов где глобальный контекст полезен.
        НЕ для login/search: там они создают FP.
        """
        if step_type not in _TITLE_INJECT_STEP_TYPES:
            return []
        return self._title_toks[:8]

    def has_sentences(self) -> bool:
        return bool(self._sents)
