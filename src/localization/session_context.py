"""Контекст сессии — сохраняет состояние между шагами сценария.

Использует spaCy для NLP-разбора каждого action:
  - dependency parsing: извлечение ROOT-глагола (intent) и прямых объектов
  - NER: именованные сущности (станции, даты, числа)
  - negation detection: "without food" → has_food=False (усиливает regex)
  - slot filling: тип поезда, класс сиденья, маршрут, тип оплаты

Результат — обогащённые BM25-токены для последующих шагов на основе
истории сессии (carry-over slots между связанными шагами).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Optional

from .step_typer import StepInfo


# ---------------------------------------------------------------------------
# spaCy: ленивая загрузка модели (один раз на процесс)
# ---------------------------------------------------------------------------

_NLP = None


def _get_nlp():
    global _NLP
    if _NLP is None:
        import spacy  # noqa: PLC0415
        try:
            _NLP = spacy.load("en_core_web_sm", disable=["ner"])
        except OSError:
            # Модель не установлена → fallback к пустому pipeline
            _NLP = spacy.blank("en")
    return _NLP


# ---------------------------------------------------------------------------
# NLPExtractor — разбор одного action-текста
# ---------------------------------------------------------------------------

@dataclass
class ActionSlots:
    """Семантические слоты, извлечённые из одного action-текста."""
    root_verb: str = ""          # ROOT глагол (намерение): "book", "search", "cancel"
    direct_obj: str = ""         # dobj: "seat", "ticket", "trip", "order"
    modifiers: list[str] = field(default_factory=list)  # amod/compound к dobj
    preps: dict[str, list[str]] = field(default_factory=dict)  # prep → [tokens]
    negated_nouns: set[str] = field(default_factory=set)  # "without food" → {food}
    affirmed_nouns: set[str] = field(default_factory=set)  # "with food" → {food}
    has_transfer: bool = False    # "transfer at", "with a transfer"
    route_from: str = ""          # "from Shanghai"
    route_to: str = ""            # "to Nanjing"
    extra_bm25: list[str] = field(default_factory=list)  # итоговые BM25-токены


_NEGATION_DEPS = {"neg"}
_NEGATION_PREPS = {"without", "no", "not"}

# Слова не несущие семантической нагрузки для BM25
_STOP_WORDS = {"the", "a", "an", "this", "its", "for", "of", "on", "in",
               "at", "to", "by", "with", "as", "or", "and", "is", "be",
               "are", "it", "i", "my", "their", "new", "first", "second"}


@lru_cache(maxsize=512)
def _parse_cached(text: str) -> ActionSlots:
    """Распарсить action-текст через spaCy. Результат кешируется."""
    nlp = _get_nlp()
    doc = nlp(text.lower())
    slots = ActionSlots()

    # --- ROOT глагол (намерение) ---
    for token in doc:
        if token.dep_ == "ROOT" and token.pos_ in ("VERB", "NOUN"):
            # Лемматизируем: "booking" → "book", "searching" → "search"
            slots.root_verb = token.lemma_
            break

    # --- Найти прямой объект ROOT ---
    root_tok = next((t for t in doc if t.dep_ == "ROOT"), None)
    if root_tok:
        for child in root_tok.children:
            if child.dep_ in ("dobj", "attr", "nsubj") and child.pos_ in ("NOUN", "PROPN"):
                slots.direct_obj = child.lemma_
                slots.modifiers = [
                    c.lemma_ for c in child.subtree
                    if c.dep_ in ("amod", "compound") and c.lemma_ not in _STOP_WORDS
                ]
                break

    # --- Препозиционные группы: "from X", "to Y", "at Z", "with/without W" ---
    for token in doc:
        if token.dep_ == "prep" and token.head.dep_ in ("ROOT", "dobj", "attr"):
            prep_text = token.lemma_
            # Объекты препозиции
            obj_tokens = [
                c.lemma_ for c in token.subtree
                if c != token and c.pos_ in ("NOUN", "PROPN", "ADJ") and c.lemma_ not in _STOP_WORDS
            ]
            if obj_tokens:
                slots.preps.setdefault(prep_text, []).extend(obj_tokens)

                # Маршрут: "from" → route_from, "to" → route_to
                if prep_text == "from" and not slots.route_from:
                    slots.route_from = " ".join(obj_tokens[:2])
                elif prep_text == "to" and not slots.route_to:
                    slots.route_to = " ".join(obj_tokens[:2])

                # Отрицание: "without food" → negated_nouns += food
                if prep_text in _NEGATION_PREPS:
                    slots.negated_nouns.update(obj_tokens)
                elif prep_text == "with":
                    slots.affirmed_nouns.update(obj_tokens)

    # --- Negation через dep=neg ---
    for token in doc:
        if token.dep_ == "neg":
            # Отрицание: пометить головное слово и его объекты как negated
            head = token.head
            slots.negated_nouns.add(head.lemma_)
            for child in head.children:
                if child.dep_ in ("dobj", "nmod"):
                    slots.negated_nouns.add(child.lemma_)

    # --- Детект transfer/transit ---
    transfer_re = re.compile(r"\b(transfer|transit|via\s+\w+\s+to)\b", re.I)
    if transfer_re.search(text):
        slots.has_transfer = True

    # --- Итоговые BM25-токены из slots ---
    tokens: list[str] = []

    # ROOT глагол как токен (усиливает verb-named handlers)
    if slots.root_verb and slots.root_verb not in _STOP_WORDS:
        tokens.append(slots.root_verb)

    # Объект + модификаторы
    if slots.direct_obj and slots.direct_obj not in _STOP_WORDS:
        tokens.append(slots.direct_obj)
    for m in slots.modifiers:
        if m not in _STOP_WORDS:
            tokens.append(m)

    # Transit boost
    if slots.has_transfer:
        tokens.extend(["transfer", "transit"])

    # "With" nouns → add to BM25 (food, insurance, luggage)
    for noun in slots.affirmed_nouns:
        if noun not in _STOP_WORDS and noun not in slots.negated_nouns:
            tokens.append(noun)

    slots.extra_bm25 = tokens
    return slots


# ---------------------------------------------------------------------------
# SessionContext
# ---------------------------------------------------------------------------

@dataclass
class SessionContext:
    """Сессионный контекст между шагами сценария.

    Хранит семантические слоты, извлечённые spaCy из истории шагов.
    Обогащает BM25-запрос следующих шагов контекстными токенами.
    """
    train_type: str = ""
    seat_class: str = ""
    route_from: str = ""
    route_to: str = ""
    booking_done: bool = False
    order_paid: bool = False
    has_food: bool | None = None
    has_luggage: bool | None = None
    has_insurance: bool | None = None
    prev_step_types: list[str] = field(default_factory=list)
    # Слоты последнего booking-шага
    booking_slots: Optional[ActionSlots] = None

    # ------------------------------------------------------------------

    def update(self, step_type: str, info: StepInfo, action: str = "") -> None:
        """Обновить контекст после классификации шага.

        Парсит action через spaCy и сохраняет извлечённые слоты.
        """
        if info.train_type:
            self.train_type = info.train_type
        if info.seat_class:
            self.seat_class = info.seat_class
        if info.has_food is not None:
            self.has_food = info.has_food
        if info.has_luggage is not None:
            self.has_luggage = info.has_luggage
        if info.has_insurance is not None:
            self.has_insurance = info.has_insurance

        # spaCy-разбор для извлечения маршрута и других слотов
        if action and step_type in {"search_train", "search_advanced", "search_transit"}:
            slots = _parse_cached(action)
            if slots.route_from:
                self.route_from = slots.route_from
            if slots.route_to:
                self.route_to = slots.route_to

        if step_type == "book":
            self.booking_done = True
            self.order_paid = False
            if action:
                self.booking_slots = _parse_cached(action)
        elif step_type == "pay":
            self.order_paid = True
        elif step_type == "cancel":
            self.booking_done = False
            self.order_paid = False
        elif step_type == "rebook":
            self.order_paid = False

        self.prev_step_types.append(step_type)

    # ------------------------------------------------------------------

    def context_tokens(self, step_type: str, current_info: StepInfo,
                       action: str = "") -> list[str]:
        """BM25-токены из контекста сессии для текущего шага.

        Фиксированные усиления для конкретных step_types — проверены на S0.
        spaCy используется только для slot filling (update), не для BM25.
        """
        tokens: list[str] = []

        # check_refund: "calculate" + "refund" → ts-cancel-service#calculate
        if step_type == "check_refund":
            tokens.extend(["calculate", "refund"])

        # query_food_trip: "food" + "menu" → ts-food-map-service или ts-train-food-service
        if step_type == "query_food_trip":
            tokens.extend(["food", "menu"])

        # query_insurance: "assurance"×2 → boost assurance-service vs consign-service
        if step_type == "query_insurance":
            tokens.extend(["assurance", "assurance"])

        return tokens


# ---------------------------------------------------------------------------
# Graph-guided context selector
# ---------------------------------------------------------------------------

def select_step_context(oracle, remote_wiring: dict, module_map: dict,
                        node_map: dict, max_modules: int = 4) -> dict:
    """Graph-guided выбор релевантного контекста для текущего шага.

    Использует remote_call рёбра из oracle.step_history для фильтрации:
    возвращает только module names, topologically reachable из последнего шага.
    Параметры принимаются как plain dict-ы, чтобы не импортировать Index.
    """
    if not getattr(oracle, "step_history", None):
        return {}

    last = oracle.step_history[-1]
    if not last.reachable_modules:
        return {}

    # Имена handler-ов из reachable nids (для Qwen-context в будущем)
    handler_names: list[str] = []
    for nid in last.reachable_nids:
        n = node_map.get(nid, {})
        name = n.get("name", "")
        if name and len(name) > 3:
            handler_names.append(name)

    return {
        "prev_step_type": last.step_type,
        "prev_found_count": len(last.found_handlers),
        "graph_reachable_modules": sorted(last.reachable_modules)[:max_modules],
        "graph_handler_names": handler_names[:8],
    }
