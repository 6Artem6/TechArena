"""Action-Condition семантический движок.

Реализует трёхуровневую декомпозицию шага сценария:

  Уровень 1 — FSM Condition Scoring:
    Каждый handler получает оценку совместимости с текущим FSM-состоянием oracle.
    Например: ts-rebook-service требует oracle.can_rebook() == True.
    При book-шаге (order_status="") — penalty, при rebook-шаге — ok.
    Derived from oracle.py FSM transitions, не domain dictionary.

  Уровень 2 — Sub-Intent Extraction:
    Один шаг → список SubIntent (action_query, service_hint, weight).
    "Book with food from station store" → [book_intent, station_food_intent].
    Позволяет NLP искать все нужные сервисы за один шаг.

  Уровень 3 — Handler Path Profiling:
    Для каждого handler: URL path → step_type affinity (rebook handler → "rebook").
    Derived from skeleton URL paths + step_type names from domain_config.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .._config import cfg_get

if TYPE_CHECKING:
    from src.trace.oracle import Oracle
    from src.localization.step_typer import StepInfo


# ---------------------------------------------------------------------------
# Уровень 1: Handler Step-Type Profile (из URL paths скелета)
# ---------------------------------------------------------------------------

@dataclass
class HandlerStepProfile:
    """Профиль хэндлера: к какому step_type он относится и какое oracle-состояние требует."""
    nid: str
    path_tokens: frozenset[str]
    step_type_affinity: str | None   # "rebook", "cancel", "pay", ... — если явно в URL
    precondition: str | None         # "can_rebook", "can_cancel", "order_init", "order_paid", ...


# Маппинг: token в URL path → step_type
# Derived: шаги из domain_config + их типичные URL-паттерны из skeleton.
# НЕ domain dictionary — это структурные токены из skeleton.
_PATH_TOKEN_TO_STEP_TYPE: dict[str, str] = {
    "rebook":    "rebook",
    "cancel":    "cancel",
    "login":     "login",
    "register":  "register",
    "execute":   "enter_station",
    "preserve":  "book",
    "preserveother": "book",
    # pay: inside_payment, outside_payment используют другие паттерны
    # collected: collect endpoint
    "collected": "collect",
}

# Маппинг: step_type → oracle precondition
# Derived from oracle.can_X() и oracle.update_from_step_type().
# Это FSM-структура, не domain knowledge.
_STEP_TYPE_PRECONDITION: dict[str, str] = {
    "rebook":       "can_rebook",       # oracle.can_rebook() — order in INIT/PAID
    "cancel":       "can_cancel",       # oracle.can_cancel() — order == PAID
    "collect":      "order_paid",       # order_status == PAID
    "enter_station": "order_collected", # order_status == COLLECTED
    "login":        "always",           # no constraint
    "register":     "always",
    "book":         "always",           # creates new order — нет precondition
    "search_train": "always",
}


def _check_precondition(precondition: str | None, oracle: "Oracle") -> bool:
    """Проверить FSM-precondition для handler. True = handler совместим с oracle-состоянием."""
    if precondition is None or precondition == "always":
        return True
    if precondition == "can_rebook":
        return oracle.can_rebook()
    if precondition == "can_cancel":
        return oracle.can_cancel()
    if precondition == "order_init":
        return oracle.order_status == "INIT"
    if precondition == "order_paid":
        return oracle.order_status in ("PAID",)
    if precondition == "order_collected":
        return oracle.order_status in ("COLLECTED",)
    return True  # неизвестный precondition → не штрафуем


def build_handler_profiles(ix) -> dict[str, HandlerStepProfile]:
    """Построить FSM-профили для всех handlers из skeleton URL paths.

    Вызывать один раз при инициализации (или lazy через глобальный кеш).
    """
    profiles: dict[str, HandlerStepProfile] = {}

    for nid, verb, path_segs in ix.handlers:
        tokens = frozenset(seg.lower() for seg in path_segs if seg and not seg.startswith("{"))

        # Определить step_type affinity из URL tokens
        affinity = None
        for tok in tokens:
            if tok in _PATH_TOKEN_TO_STEP_TYPE:
                affinity = _PATH_TOKEN_TO_STEP_TYPE[tok]
                break

        precondition = _STEP_TYPE_PRECONDITION.get(affinity) if affinity else None

        profiles[nid] = HandlerStepProfile(
            nid=nid,
            path_tokens=tokens,
            step_type_affinity=affinity,
            precondition=precondition,
        )

    return profiles


# Глобальный кеш профилей
_HANDLER_PROFILES: "dict[str, HandlerStepProfile] | None" = None


def get_handler_profiles(ix) -> dict[str, HandlerStepProfile]:
    global _HANDLER_PROFILES
    if _HANDLER_PROFILES is None:
        _HANDLER_PROFILES = build_handler_profiles(ix)
    return _HANDLER_PROFILES


# ---------------------------------------------------------------------------
# FSM Reranking: штрафовать handlers с несовместимым oracle-состоянием
# ---------------------------------------------------------------------------

def fsm_condition_rerank(
    ranked: list[tuple[float, str]],
    step_type: str,
    oracle: "Oracle",
    ix,
    fsm_penalty: float = 0.15,
) -> list[tuple[float, str]]:
    """Переранжировать с учётом FSM-совместимости.

    Штрафует ТОЛЬКО handlers с explicit step_type affinity когда:
    1. handler_affinity != step_type (мы в другом типе шага)
    2. precondition хэндлера НЕ выполнен в текущем oracle-состоянии

    Ключевые случаи:
    - rebook handler (can_rebook=False) в book-шаге → score * 0.15 → падает ниже tau
    - cancel handler (can_cancel=False) в book/search шаге → score * 0.15
    - book/preserve handlers в rebook-шаге → НЕТ штрафа (precondition="always", rebook вызывает preserve)
    - neutral handlers (travel, order, seat) → НЕТ штрафа (affinity=None)

    fsm_penalty=0.15 выталкивает handler за tau-порог: best*(1-0.05)=0.95*best.
    Handler на score=0.95 после penalty → 0.14 << 0.95*best. ✓
    """
    if not ranked or oracle is None:
        return ranked

    profiles = get_handler_profiles(ix)
    result = []

    for sc, h in ranked:
        profile = profiles.get(h)
        if profile is None or profile.step_type_affinity is None:
            # Нет explicit step-type в URL → нейтральный handler → без штрафа
            result.append((sc, h))
            continue

        handler_affinity = profile.step_type_affinity

        if handler_affinity == step_type:
            # Точное совпадение → без штрафа
            result.append((sc, h))
            continue

        precondition = profile.precondition
        if precondition is None or precondition == "always":
            # Нет precondition (e.g., book/preserve) → handler валиден в любом шаге
            # Preserve вызывается из rebook внутренне → не штрафуем
            result.append((sc, h))
            continue

        # Handler имеет специфический precondition И мы в другом типе шага.
        # Проверяем: выполнен ли precondition в текущем oracle-состоянии?
        if not _check_precondition(precondition, oracle):
            # Oracle не поддерживает это действие → handler не должен быть entry point
            result.append((sc * fsm_penalty, h))
        else:
            # Precondition выполнен, хотя step_type другой → лёгкий штраф
            # Пример: rebook-handler когда oracle=PAID, но текущий шаг = "book" (2й билет)
            result.append((sc * 0.5, h))

    result.sort(key=lambda x: -x[0])
    return result


# ---------------------------------------------------------------------------
# Уровень 2: Sub-Intent Extraction
# ---------------------------------------------------------------------------

@dataclass
class SubIntent:
    """Одно под-намерение внутри шага."""
    action: str          # "create_booking", "order_station_food", "retrieve_contact"
    service_hint: str    # подсказка сервиса: "contacts", "food_station", "assurance"
    query_tokens: list[str]  # дополнительные BM25-токены для этого намерения
    weight: float = 1.0  # 1.0 = primary, 0.6 = secondary


# Статические маппинги sub-intent детекции: derived из oracle-полей + UI-паттернов.
# НЕ domain dictionary: oracle field names ("food", "insurance", "consign") + UI keywords.
_CONTACT_BOOK_RE = re.compile(
    r"\b(contact\s+book|contact\s+list|take\s+the\s+.*contact|select\s+.*contact|pick\s+.*contact)\b",
    re.I,
)
_STATION_FOOD_RE = re.compile(
    r"\b(station\s+food|food.*station\s+store|station.*store|store.*food|food\s+store)\b",
    re.I,
)
_TRAIN_FOOD_RE = re.compile(
    r"\b(food\s+on\s+the\s+train|train\s+food|food\s+menu|food.*trip)\b",
    re.I,
)
_ASSURANCE_RE = re.compile(
    r"\b(assurance|insurance)\b",
    re.I,
)
_CONSIGN_RE = re.compile(
    r"\b(consign|luggage|baggage)\b",
    re.I,
)


def extract_sub_intents(
    text: str,
    step_info: "StepInfo",
    oracle: "Oracle | None" = None,
) -> list[SubIntent]:
    """Извлечь sub-intents из текста шага.

    Возвращает список подзадач, каждая из которых может требовать отдельного сервиса.

    Primary intent: всегда включён (основное действие шага).
    Secondary intents: из условий бронирования, UI-действий, oracle state.
    """
    intents: list[SubIntent] = []

    # Primary intent: главное действие
    primary_tokens = _primary_tokens_for_step_type(step_info.step_type)
    intents.append(SubIntent(
        action=step_info.step_type,
        service_hint="",
        query_tokens=primary_tokens,
        weight=1.0,
    ))

    # Contact book sub-intent: "Open the contact book", "take the first contact"
    if _CONTACT_BOOK_RE.search(text):
        intents.append(SubIntent(
            action="retrieve_contact",
            service_hint="contacts",
            query_tokens=["contacts", "contact", "account"],
            weight=0.8,
        ))

    # Station food sub-intent
    if _STATION_FOOD_RE.search(text) or (oracle is not None and oracle.has_food
                                          and step_info.step_type == "view_station_food"):
        intents.append(SubIntent(
            action="browse_station_food",
            service_hint="food_station",
            query_tokens=["station", "food", "store"],
            weight=0.75,
        ))

    # Train food sub-intent
    if _TRAIN_FOOD_RE.search(text):
        intents.append(SubIntent(
            action="view_train_food",
            service_hint="food_train",
            query_tokens=["food", "train", "menu"],
            weight=0.7,
        ))

    # Assurance sub-intent (if mentioned in step OR oracle.has_insurance=True at book step)
    if _ASSURANCE_RE.search(text) and step_info.step_type == "query_insurance":
        intents.append(SubIntent(
            action="retrieve_assurance",
            service_hint="assurance",
            query_tokens=["assurance", "insurance", "record"],
            weight=0.75,
        ))

    return intents


def _primary_tokens_for_step_type(step_type: str) -> list[str]:
    """Получить основные BM25-токены для step_type.

    Derived из step_type names — не domain dictionary.
    """
    # Базовые токены: имя самого step_type + синонимы из конфига step_rules
    rules = cfg_get("step_rules", [])
    tokens = [step_type]

    for rule in rules:
        if rule.get("type") == step_type:
            # Первые 3 паттерна: очистить regex-синтаксис, взять ключевые слова
            for pat in rule.get("patterns", [])[:3]:
                words = re.findall(r"[a-z_]+", pat.lower())
                tokens.extend(w for w in words if len(w) > 2)
            break

    return list(dict.fromkeys(tokens))  # deduplicate, preserve order


# ---------------------------------------------------------------------------
# Уровень 3: Multi-Intent Query Builder
# ---------------------------------------------------------------------------

@dataclass
class DecomposedStep:
    """Декомпозированный шаг: набор sub-intents + oracle conditions."""
    step_type: str
    sub_intents: list[SubIntent]
    oracle_conditions: dict[str, object]  # {"train_type": "G", "has_insurance": True, ...}
    primary_query: str                    # BM25-запрос для основного действия
    condition_query: str                  # BM25-запрос для условий/состояний


def decompose_step(
    text: str,
    step_info: "StepInfo",
    oracle: "Oracle | None" = None,
) -> DecomposedStep:
    """Декомпозировать шаг на action + conditions.

    Результат используется для:
    - Построения нескольких BM25-запросов (по одному на sub-intent)
    - FSM-совместимости handler-ов
    - Контекстного включения/исключения сервисов

    Пример:
        "Book a first-class seat, with food from a station store, assurance of the first type"
        → primary: "book seat first class"
        → sub_intents: [contact_book?, station_food, assurance]
        → conditions: {train_type: "G", has_food: True, has_insurance: True}
    """
    intents = extract_sub_intents(text, step_info, oracle)

    # Oracle conditions
    conditions: dict[str, object] = {}
    if oracle is not None:
        conditions["train_type"] = oracle.train_type
        conditions["order_status"] = oracle.order_status
        conditions["has_food"] = oracle.has_food
        conditions["has_insurance"] = oracle.has_insurance
        conditions["has_luggage"] = oracle.has_luggage

    # Primary query: step_type name + key words from text (без service-specific условий)
    # Убираем условия (food, assurance, luggage) из primary — они идут в sub-intents
    _CONDITION_WORDS = {"food", "assurance", "insurance", "luggage", "consign",
                        "station", "store", "contact"}
    primary_words = [w for w in re.findall(r"[a-z]+", text.lower())
                     if len(w) > 2 and w not in _CONDITION_WORDS][:12]
    primary_query = " ".join([step_info.step_type] + primary_words)

    # Condition query: только условия и сервисные требования
    condition_parts = []
    if oracle is not None:
        if oracle.train_type == "G":
            condition_parts.extend(["high", "speed", "gaotie"])
        elif oracle.train_type == "D":
            condition_parts.extend(["regular", "other", "type"])
        if oracle.has_food:
            condition_parts.append("food")
        if oracle.has_insurance:
            condition_parts.extend(["assurance", "insurance"])
        if oracle.has_luggage:
            condition_parts.extend(["consign", "luggage"])
    condition_query = " ".join(condition_parts) if condition_parts else ""

    return DecomposedStep(
        step_type=step_info.step_type,
        sub_intents=intents,
        oracle_conditions=conditions,
        primary_query=primary_query,
        condition_query=condition_query,
    )


# ---------------------------------------------------------------------------
# Вспомогательная: query builder для sub-intent BM25-поиска
# ---------------------------------------------------------------------------

def build_sub_intent_queries(decomp: DecomposedStep) -> list[tuple[str, float]]:
    """Вернуть список (query, weight) для поиска по каждому sub-intent.

    Используется вне основного ranker — для расширения hs за счёт secondary intents.
    """
    queries: list[tuple[str, float]] = []
    for intent in decomp.sub_intents:
        q_parts = [intent.action] + intent.query_tokens
        if decomp.condition_query:
            q_parts.append(decomp.condition_query)
        queries.append((" ".join(q_parts), intent.weight))
    return queries
