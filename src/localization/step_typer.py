"""Фаза 2.1 — Step Typing: классификация шага сценария.

Принимает action-текст шага → возвращает StepInfo.
При наличии StepParams от Qwen (Pass 1) использует их как основной сигнал;
иначе — regex-based fallback.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .._config import cfg_get

if TYPE_CHECKING:
    from src.localization.qwen_encoder import StepParams


# Шаги, для которых Qwen keywords безопасно инжектировать в extra_tokens.
# Для search/* и view_trip keywords ("cheapest", "trips") сдвигают BM25 вредно.
_SAFE_KEYWORD_TYPES = frozenset({
    "book", "pay", "cancel", "collect", "rebook",
    "enter_station", "add_consign", "query_consign",
    "check_price", "check_refund",
})


# ---------------------------------------------------------------------------
# Паттерны типа поезда (из domain_config.yaml → step_train_patterns)
#
# ВНИМАНИЕ: DongChe здесь G-диапазон для BM25-disambiguation.
# В oracle.py DongChe — D-тип (разные роли!).
# ---------------------------------------------------------------------------

def _build_train_re(key: str, default_alts: list[str]) -> re.Pattern[str]:
    """Собрать regex из списка альтернатив (из конфига или дефолта)."""
    raw = cfg_get("step_train_patterns", {})
    alts = raw.get(key, default_alts)
    pattern = r"\b(" + "|".join(alts) + r")\b"
    return re.compile(pattern, re.I)


_G_TRAIN = _build_train_re("G", [
    r"high.?speed", r"gaotie", r"gao.?tie", r"g.?train", r"g-?\s*type",
    r"dongche", r"dong.?che", r"d.?train", r"d-?\s*type", r"crh", r"bullet",
])
_OTHER_TRAIN = _build_train_re("OTHER", [
    r"regular", r"'?other'?\s*type", r"non.?high.?speed", r"slow", r"normal\s+train",
])

# NOT_G: отрицание high-speed — приоритет над _G_TRAIN (как в oracle.py)
# Предотвращает false positive: "not high-speed" → train_type=G
_NOT_G_TRAIN = re.compile(
    r"(not\s+high.?speed|regular\s+train|other\b.{0,30}\btype)",
    re.I,
)

_SEAT_FIRST  = re.compile(r"\b(first.?class|business.?class|1st.?class)\b", re.I)
_SEAT_SECOND = re.compile(r"\b(second.?class|economy|2nd.?class|standard)\b", re.I)

_HAS_FOOD    = re.compile(r"\b(with\s+food|order\s+food|food\s+on\s+the\s+train|station\s+food)\b", re.I)
_NO_FOOD     = re.compile(r"\b(no\s+food|without\s+food)\b", re.I)
_HAS_INS     = re.compile(r"\bwith\b.{0,80}\b(assurance|insurance)\b", re.I)
_NO_INS      = re.compile(r"\b(without|no)\b.{0,80}\b(assurance|insurance)\b", re.I)
_HAS_LUG     = re.compile(r"\b(with\s+luggage|luggage\s+service|consign)\b", re.I)
_NO_LUG      = re.compile(r"\b(no\s+luggage|without\s+luggage)\b", re.I)

# Оплата: "internal" без признаков external → inside-payment; "external" или "empty balance" → outside-payment.
_PAY_INTERNAL = re.compile(r"\binternal\b", re.I)
_PAY_EXTERNAL = re.compile(r"\bexternal\b", re.I)
_PAY_EMPTY    = re.compile(r"\binternal\b.{0,50}\bempty\b", re.I)


# ---------------------------------------------------------------------------
# Правила классификации шагов (из domain_config.yaml → step_rules)
# ---------------------------------------------------------------------------

def _load_step_rules() -> list[tuple[str, list[str]]]:
    """Загрузить правила из конфига (fallback когда Qwen недоступен или вернул 'other')."""
    raw = cfg_get("step_rules", [])
    return [(r["type"], r["patterns"]) for r in raw]


_STEP_RULES: list[tuple[str, list[str]]] = _load_step_rules()


# ---------------------------------------------------------------------------
# Dataclass результата
# ---------------------------------------------------------------------------

_SYSTEM_REFUSES = re.compile(r'\bthe\s+system\s+(refuses|rejects|denies)\b', re.I)


@dataclass
class StepInfo:
    step_type: str = "unknown"
    train_type: str = ""         # "G" | "OTHER" | ""
    seat_class: str = ""         # "FIRST" | "SECOND" | ""
    has_food: bool | None = None
    has_insurance: bool | None = None
    has_luggage: bool | None = None
    is_refused: bool = False     # "the system refuses" — шаг отклонён системой
    # Дополнительные BM25-токены, выведенные из типа шага
    extra_tokens: list[str] = field(default_factory=list)


def _apply_train_type(text: str, info: StepInfo) -> None:
    if _NOT_G_TRAIN.search(text):
        info.train_type = "OTHER"
    elif _G_TRAIN.search(text):
        info.train_type = "G"
    elif _OTHER_TRAIN.search(text):
        info.train_type = "OTHER"


def _apply_seat_class(text: str, info: StepInfo) -> None:
    if _SEAT_FIRST.search(text):
        info.seat_class = "FIRST"
    elif _SEAT_SECOND.search(text):
        info.seat_class = "SECOND"


def _apply_food(text: str, info: StepInfo) -> None:
    if _HAS_FOOD.search(text) and not _NO_FOOD.search(text):
        info.has_food = True
    elif _NO_FOOD.search(text):
        info.has_food = False


def _apply_insurance(text: str, info: StepInfo) -> None:
    if _HAS_INS.search(text) and not _NO_INS.search(text):
        info.has_insurance = True
    elif _NO_INS.search(text):
        info.has_insurance = False


def _apply_luggage(text: str, info: StepInfo) -> None:
    if _HAS_LUG.search(text) and not _NO_LUG.search(text):
        info.has_luggage = True
    elif _NO_LUG.search(text):
        info.has_luggage = False


def _apply_options(text: str, info: StepInfo) -> None:
    _apply_food(text, info)
    _apply_insurance(text, info)
    _apply_luggage(text, info)


def classify(action: str, step_params: "StepParams | None" = None) -> StepInfo:
    """Классифицировать текст шага, вернуть StepInfo.

    step_params: StepParams от Qwen Pass 1 (основной путь).
    Если None — regex-based fallback.
    """
    text = action.lower()
    info = StepInfo()

    # is_refused: шаг явно помечён как отклонённый системой → не добавлять сервис в trace
    if _SYSTEM_REFUSES.search(action):
        info.is_refused = True

    # train_type — всегда regex: D-train→G mapping намеренный, Qwen не знает этот трюк.
    # step_type — Qwen primary (NL-понимание шире regex), regex как fallback.
    _apply_train_type(text, info)
    _apply_seat_class(text, info)

    if step_params is not None and step_params.step_type not in ("other", "unknown", ""):
        info.step_type = step_params.step_type
    else:
        for step_type, keywords in _STEP_RULES:
            for kw in keywords:
                if re.search(kw, text, re.I):
                    info.step_type = step_type
                    break
            if info.step_type != "unknown":
                break

    if step_params is not None:
        # --- Qwen path: структурные атрибуты бронирования ---
        # services: food_train, food_station, insurance, luggage
        food_train   = step_params.get_service("food_train")
        food_station = step_params.get_service("food_station")
        if food_train is True or food_station is True:
            info.has_food = True
        elif food_train is False and food_station is False:
            info.has_food = False
        else:
            _apply_food(text, info)

        ins = step_params.get_service("insurance")
        if ins is not None:
            info.has_insurance = ins
        else:
            _apply_insurance(text, info)

        lug = step_params.get_service("luggage")
        if lug is not None:
            info.has_luggage = lug
        else:
            _apply_luggage(text, info)

    else:
        # --- Regex fallback (Qwen недоступен) ---
        _apply_options(text, info)

    info.extra_tokens = _build_extra_tokens(info, action, step_params)
    return info


def _build_extra_tokens(
    info: StepInfo,
    action: str = "",
    step_params: "StepParams | None" = None,
) -> list[str]:
    """Формирует структурные BM25-токены из параметров бронирования.

    Семантический мост NL → code (action vocabulary) заменён dense-векторами
    (SemanticReranker / BGE-M3) в src/similarity/encoder.py.
    """
    tokens: list[str] = []

    # Структурные атрибуты бронирования: извлекаются из текста шага напрямую
    if info.seat_class == "FIRST":
        tokens.append("first class")
    elif info.seat_class == "SECOND":
        tokens.append("second class")

    if info.has_food is True:
        tokens.append("food")
    if info.has_insurance is True:
        tokens.append("insurance")
    if info.has_luggage is True:
        tokens.append("consign luggage")

    # step_type-специфичные токены: усиливают нужный handler в пределах одного сервиса
    # search_transit: "transfer"×2 + "via" → boost getTransferResult vs queryOrdersForRefresh
    if info.step_type == "search_transit":
        tokens.extend(["transfer", "transfer", "via"])
    # cancel: "ticket" + "verify" → boost cancelTicket (имеет оба) vs calculate (не имеет)
    if info.step_type == "cancel":
        tokens.extend(["ticket", "verify"])
    # rebook: "rebook" x2 extra → triple "rebook" в query; rebook.rebook имеет TF=3 vs payDifference TF=2
    if info.step_type == "rebook":
        tokens.extend(["rebook", "rebook"])

    # pay: использовать payment_mode из StepParams если доступен, иначе regex
    if info.step_type == "pay":
        _pm = step_params.payment_mode if step_params is not None else ""
        if _pm == "external" or (not _pm and (_PAY_EXTERNAL.search(action) or _PAY_EMPTY.search(action))):
            tokens.extend(["payment", "payment", "service"])
        elif _pm == "internal" or (not _pm and _PAY_INTERNAL.search(action)):
            tokens.append("inside")

    # check_price: "price"×2 — усилить сигнал price vs order (queryOrdersForRefresh шумит)
    if info.step_type == "check_price":
        tokens.extend(["price", "price"])

    # query_insurance: "assurance"×2 — boost assurance-service vs consign-service
    # Без этого "record of the order" тянет к consign-service (findByOrderId)
    if info.step_type == "query_insurance":
        tokens.extend(["assurance", "assurance"])

    # update_consign: "update" extra → updateConsign score > insertConsign score →
    # разделяет update vs insert в tau-window (иначе оба в hs → insertConsign FP).
    if info.step_type == "update_consign":
        tokens.extend(["consign", "consign", "update"])
    # add_consign: "insert" extra → insertConsign score > updateConsign.
    if info.step_type == "add_consign":
        tokens.extend(["consign", "consign", "insert"])
    # query_consign: "account" в action → findByAccountId; иначе — общий consign query.
    if info.step_type == "query_consign":
        tokens.extend(["consign", "consign"])
        if "account" in action.lower():
            tokens.append("account")

    # get_contact: "account"×2 → findContactsByAccountId wins over getContactsByContactsId.
    # Contacts lookup in booking flow is always by account ID, not by contacts ID.
    if info.step_type == "get_contact":
        tokens.extend(["account", "account"])

    # enter_station: "execute"×2 → executeTicket (URL has "execute" twice) wins over preserve/book handlers.
    if info.step_type == "enter_station":
        tokens.extend(["execute", "execute"])

    # collect: "collect"×2 → collectTicket wins over executeTicket.
    if info.step_type == "collect":
        tokens.extend(["collect", "collect"])

    # query_orders: "refresh/filter/view" → tokens ["query", "order", "orders"] →
    # усиливает queryOrders, getAllOrders в order-service (1 overlap → 3 overlap).
    if info.step_type == "query_orders":
        tokens.extend(["query", "order", "orders"])

    # check_refund: "calculate"×2 — boost ts-cancel-service/calculate над прочими
    # "refund" из action уже есть в query; "calculate" добавляет прямой сигнал сервиса
    if info.step_type == "check_refund":
        tokens.extend(["calculate", "refund"])

    # Qwen keywords: инжектировать только для безопасных step_types
    if (step_params is not None
            and step_params.keywords
            and info.step_type in _SAFE_KEYWORD_TYPES):
        for kw in step_params.keywords:
            if kw and kw not in tokens:
                tokens.append(kw)

    return tokens
