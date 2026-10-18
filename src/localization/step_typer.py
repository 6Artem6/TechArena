"""Фаза 2.1 — Step Typing: классификация шага сценария.

Принимает action-текст шага → возвращает (step_type, params).
step_type используется для:
  - добавления дополнительных BM25-токенов при поиске entry point
  - disambiguation: travel vs travel2, order vs orderOther (P-75, P-54)
  - Oracle.parse_step() уточнение параметров
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .._config import cfg_get


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
    """Загрузить правила из конфига; fallback — встроенный список."""
    raw = cfg_get("step_rules", [])
    if raw:
        return [(r["type"], r["patterns"]) for r in raw]
    # Встроенный fallback (дублирует YAML для случая отсутствия конфига)
    return [
        ("query_consign",  ["look up.*consign", "look up.*luggage",
                            "consignment record", "luggage consignment.*registered",
                            r"check what.*consign", "list of.*consign"]),
        ("add_consign",    [r"^add.*consign", r"^register.*consign"]),
        ("register",       [r"^register\b", "sign up", "create account",
                            r"\bnew account\b", "new passenger"]),
        ("admin_login",    ["admin console"]),
        ("login",          ["log in", "login", "sign in"]),
        ("book",           [r"^book\b", r"^purchase\b", r"^reserve\b", r"^buy\b",
                            "order a ticket"]),
        ("pay",            [r"^pay\b", "payment"]),
        ("cancel",         [r"^cancel\b"]),
        ("rebook",         ["rebook", "change seat", "change ticket", "change the trip"]),
        ("collect",        [r"^collect\b", "pick up", "take the ticket"]),
        ("enter_station",  [r"enter.+station", r"\bboard.+train", r"\bboard.+order"]),
        ("search_advanced", ["^advanced search", r"cheapest\s+trips", r"quickest\s+trips",
                             "fewest.stop"]),
        ("search_train",   ["search", "find train", "find trip", "trips from",
                            "trains from", "departing"]),
        ("view_trip",      ["open the details", "details of the", "first trip found",
                            "trip found", "cheapest trip found", "open.*order"]),
        ("query_food_trip", ["food menu", "food offered on", "meal"]),
        ("browse_food_station", ["food store", "browse food", "station food",
                                 r"food at .* station"]),
        ("add_contact",    ["add a contact", "add a new contact", r"^add.*contact\b"]),
        ("get_contact",    ["contact book", "first contact", "open.*contact",
                            "take.*contact"]),
        ("query_orders",   ["list of orders", "refresh.*order", "orders.*high.speed",
                            "orders.*regular", "orders.*paid", "order.*status",
                            "view.*orders", "orders for"]),
        ("add_station",    ["add.*station", "new station"]),
        ("add_train_type", ["add.*train type", "new train type", "gaoties"]),
        ("add_route",      ["add.*route", "new route"]),
        ("add_trip",       ["add.*trip", "open.*trip", "new trip"]),
        ("query_orders",   ["paid", "filter.*paid", "paid.*filter"]),
    ]


_STEP_RULES: list[tuple[str, list[str]]] = _load_step_rules()


# ---------------------------------------------------------------------------
# Dataclass результата
# ---------------------------------------------------------------------------

@dataclass
class StepInfo:
    step_type: str = "unknown"
    train_type: str = ""         # "G" | "OTHER" | ""
    seat_class: str = ""         # "FIRST" | "SECOND" | ""
    has_food: bool | None = None
    has_insurance: bool | None = None
    has_luggage: bool | None = None
    # Дополнительные BM25-токены, выведенные из типа шага
    extra_tokens: list[str] = field(default_factory=list)


def classify(action: str) -> StepInfo:
    """Классифицировать текст шага, вернуть StepInfo."""
    text = action.lower()
    info = StepInfo()

    # --- Определить train_type ---
    # NOT_G проверяется первым: "not high-speed" не должно давать train_type=G
    if _NOT_G_TRAIN.search(text):
        info.train_type = "OTHER"
    elif _G_TRAIN.search(text):
        info.train_type = "G"
    elif _OTHER_TRAIN.search(text):
        info.train_type = "OTHER"

    # --- Определить seat_class ---
    if _SEAT_FIRST.search(text):
        info.seat_class = "FIRST"
    elif _SEAT_SECOND.search(text):
        info.seat_class = "SECOND"

    # --- Определить дополнительные опции ---
    if _HAS_FOOD.search(text) and not _NO_FOOD.search(text):
        info.has_food = True
    elif _NO_FOOD.search(text):
        info.has_food = False

    if _HAS_INS.search(text) and not _NO_INS.search(text):
        info.has_insurance = True
    elif _NO_INS.search(text):
        info.has_insurance = False

    if _HAS_LUG.search(text) and not _NO_LUG.search(text):
        info.has_luggage = True
    elif _NO_LUG.search(text):
        info.has_luggage = False

    # --- Определить step_type ---
    for step_type, keywords in _STEP_RULES:
        for kw in keywords:
            if re.search(kw, text, re.I):
                info.step_type = step_type
                break
        if info.step_type != "unknown":
            break

    # --- Сформировать extra_tokens для BM25 ---
    info.extra_tokens = _build_extra_tokens(info, action)

    return info


def _build_extra_tokens(info: StepInfo, action: str = "") -> list[str]:
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
    # pay: "external" или "internal ... empty" → outside-payment; "internal" без этих признаков → inside-payment
    if info.step_type == "pay":
        if _PAY_EXTERNAL.search(action) or _PAY_EMPTY.search(action):
            # "payment service" — семантический сигнал для BGE-M3: ts-payment-service (не inside)
            tokens.extend(["payment", "payment", "service"])
        elif _PAY_INTERNAL.search(action):
            tokens.append("inside")

    # check_price: "price"×2 — усилить сигнал price vs order (queryOrdersForRefresh шумит)
    if info.step_type == "check_price":
        tokens.extend(["price", "price"])

    # query_insurance: "assurance"×2 — boost assurance-service vs consign-service
    # Без этого "record of the order" тянет к consign-service (findByOrderId)
    if info.step_type == "query_insurance":
        tokens.extend(["assurance", "assurance"])

    # update_consign / add_consign: "consignment" в тексте → ['consignment'] ≠ ['consign'] в handler-ах
    # (updateConsign, insertConsign). Явный "consign" устраняет разрыв без влияния на query_consign.
    if info.step_type in ("update_consign", "add_consign"):
        tokens.extend(["consign", "consign"])

    # check_refund: "calculate"×2 — boost ts-cancel-service/calculate над прочими
    # "refund" из action уже есть в query; "calculate" добавляет прямой сигнал сервиса
    if info.step_type == "check_refund":
        tokens.extend(["calculate", "refund"])

    return tokens
