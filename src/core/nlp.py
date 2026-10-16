"""Текстовая обработка для NLP-локализации entry points.

Реализует те же алгоритмы что baseline_nlp.py, без зависимости от baseline.
Все функции — точные реализации оригинала (поведение идентично).
"""
from __future__ import annotations

import re


# ---------------------------------------------------------------------------
# Токенизация
# ---------------------------------------------------------------------------

def toks(t: str) -> list[str]:
    """CamelCase + underscore split → нижний регистр, len > 2.

    Идентично baseline_closure.toks():
      "createOrder" → ["create", "order"]
      "getServiceUrl" → ["get", "service", "url"]
      "HTTPClient" → ["http", "client"]
    """
    return [
        w.lower()
        for w in re.findall(
            r"[A-Za-z][a-z0-9]+|[A-Z]+(?![a-z])",
            re.sub(r"([a-z])([A-Z])", r"\1 \2", t or ""),
        )
        if len(w) > 2
    ]


_VOWELS = set("aeiouy")


def stem(w: str) -> str:
    """Консервативный стеммер.

    trains → train, stores → store, details → detail,
    departing → depart, cancelled → cancel, booked → book.
    Не трогает короткие основы (speed, paid) и не режет
    -ers/-ment/-ure (orders → order, payment).
    Идентично baseline_nlp.stem().
    """
    if len(w) >= 5 and w.endswith("ies"):
        return w[:-3] + "y"
    if (
        len(w) >= 5 and w.endswith("es") and w[-3] in "sxz"
        or w.endswith(("ches", "shes"))
    ):
        w = w[:-2]
    elif len(w) >= 4 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        w = w[:-1]
    for suf in ("ing", "ed"):
        if (
            w.endswith(suf)
            and len(w) - len(suf) >= 4
            and any(c in _VOWELS for c in w[: -len(suf)])
        ):
            w = w[: -len(suf)]
            if len(w) > 4 and w[-1] == w[-2] and w[-1] not in _VOWELS:
                w = w[:-1]
            break
    return w


def ntoks(t: str) -> list[str]:
    """toks() + stem() на каждый токен."""
    return [stem(w) for w in toks(t)]


# ---------------------------------------------------------------------------
# Запросная обработка
# ---------------------------------------------------------------------------

_NEG = re.compile(
    r"\b(?:no|not|without|non)[- ]+([a-z][a-z-]*(?:\s+[a-z][a-z-]*){0,2}?)"
    r"(?=\s*(?:,|;|\band\b|\bor\b|\)|$))",
    re.I,
)


def query_toks(text: str) -> list[str]:
    """Токены подписи шага без отрицаемых слов.

    «no insurance» → убрать «insurance»;
    «non-high-speed» → убрать «high-speed».
    Идентично baseline_nlp.query_toks().
    """
    return ntoks(_NEG.sub(" ", text))


# ---------------------------------------------------------------------------
# Расширение запроса (CRUD-синонимы первого глагола)
# ---------------------------------------------------------------------------

_READ_VERBS = frozenset({
    "view", "look", "open", "check", "search", "browse", "ask",
    "see", "list", "show", "read", "find", "get", "query", "display",
})

VERB_SYN: dict[str, list[str]] = {
    **{v: ["get", "find", "query"] for v in _READ_VERBS},
    **{
        v: ["create", "add", "new"]
        for v in ("reserve", "create", "add", "register", "place", "submit", "make")
    },
    # book: семантические синонимы глагола (reserve, order, purchase)
    "book": ["reserve", "order", "purchase"],
    **{
        v: ["update", "modify", "edit"]
        for v in ("change", "edit", "modify", "update", "set", "rename")
    },
    **{
        v: ["delete", "remove", "cancel"]
        for v in ("cancel", "delete", "remove", "withdraw")
    },
}

ADMIN: frozenset[str] = frozenset({
    "admin", "administrator", "manager", "operator", "staff", "management",
})


def expand(q: list[str]) -> list[str]:
    """Первый глагол шага → CRUD-синонимы (вес 1).

    Идентично baseline_nlp.expand().
    """
    return q + (VERB_SYN.get(q[0], []) if q else [])


# ---------------------------------------------------------------------------
# URL path utils (нужны ui_context.py)
# ---------------------------------------------------------------------------

def path_segs(p: str) -> list[str]:
    """Разбить URL-путь по '/' с отбросом пустых сегментов и query-части."""
    return [s for s in re.split(r"[/]+", p.split("?")[0]) if s]
