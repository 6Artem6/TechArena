"""Stateful-контекст выполнения сценария.

Oracle хранит состояние между шагами: баланс, тип поезда, статус заказа и т.д.
parse_step() вытаскивает параметры из текста шага (train_type, seat_class, has_food).
update_after_step() обновляет состояние по результату шага.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from .._config import cfg_get


# ---------------------------------------------------------------------------
# Паттерны типа поезда (из domain_config.yaml → oracle_train_patterns)
#
# ВНИМАНИЕ: DongChe трактуется как D-тип (для точного pruning по BFS ветвям).
# В step_typer.py DongChe в G-диапазоне (разные роли!).
# NOT_G имеет приоритет над G: предотвращает false positive "not high-speed".
# ---------------------------------------------------------------------------

def _build_re(key: str, default_alts: list[str]) -> re.Pattern[str]:
    raw = cfg_get("oracle_train_patterns", {})
    alts = raw.get(key, default_alts)
    return re.compile(r"\b(" + "|".join(alts) + r")\b", re.I)


def _build_not_g_re() -> re.Pattern[str]:
    raw = cfg_get("oracle_train_patterns", {})
    alts = raw.get("NOT_G", [
        r"not\s+high.?speed", r"regular\s+train", r"other\b.{0,30}\btype",
    ])
    return re.compile("|".join(alts), re.I)


_TRAIN_G    = _build_re("G", [
    r"high.?speed", r"g.train", r"g-?\s*type", r"gaotie", r"gao.?tie",
])
_TRAIN_D    = _build_re("D", [
    r"d.train", r"d-?\s*type", r"dongche", r"dong.?che",
])
_TRAIN_NOT_G = _build_not_g_re()

_SEAT_1ST = re.compile(r"\b(first.?class|business.?class|1st.?class)\b", re.I)
_SEAT_2ND = re.compile(r"\b(second.?class|economy|2nd.?class|standard)\b", re.I)
_HAS_FOOD = re.compile(r"\b(with\s+food|order\s+food|food\s+service)\b", re.I)
_NO_FOOD  = re.compile(r"\b(no\s+food|without\s+food)\b", re.I)
_HAS_INS  = re.compile(r"\b(with\s+insurance|insur)\b", re.I)
_HAS_LUG  = re.compile(r"\b(with\s+luggage|luggage\s+service)\b", re.I)


# ---------------------------------------------------------------------------
# Oracle dataclass
# ---------------------------------------------------------------------------

@dataclass
class Oracle:
    account:     str  = "fdse_microservice"
    balance:     int  = 10_000
    system_date: date = field(default_factory=lambda: date(2026, 9, 15))
    trip_date:   date = field(default_factory=lambda: date(2026, 10, 1))
    train_type:  str  = ""        # "G" | "D" | ""
    seat_class:  str  = ""        # "FIRST" | "SECOND" | ""
    order_status: str = ""        # "INIT" | "PAID" | "CANCELLED" | ""
    has_food:     bool = False
    has_insurance: bool = False
    has_luggage:   bool = False
    active_services: set = field(default_factory=set)

    # ------------------------------------------------------------------
    # Разбор текста шага (Pruner 3a — детерминировано из текста)
    # ------------------------------------------------------------------

    def parse_step(self, text: str) -> "Oracle":
        """Обновить поля по тексту шага, вернуть self."""
        # Порядок приоритетов:
        # 1. "not high-speed" / "regular train" / "Other type" → D (явное отрицание G)
        # 2. "high-speed" / "GaoTie" / "G-type" → G (доминирует над DongChe)
        # 3. "DongChe" / "D-type" только без high-speed → D
        if _TRAIN_NOT_G.search(text):
            self.train_type = "D"
        elif _TRAIN_G.search(text):
            self.train_type = "G"
        elif _TRAIN_D.search(text):
            self.train_type = "D"

        if _SEAT_1ST.search(text):
            self.seat_class = "FIRST"
        elif _SEAT_2ND.search(text):
            self.seat_class = "SECOND"

        if _HAS_FOOD.search(text) and not _NO_FOOD.search(text):
            self.has_food = True
        if _HAS_INS.search(text):
            self.has_insurance = True
        if _HAS_LUG.search(text):
            self.has_luggage = True

        return self

    # ------------------------------------------------------------------
    # Обновление состояния после шага (Pruner 3b — контекстное)
    # ------------------------------------------------------------------

    def mark_order_placed(self) -> None:
        self.order_status = "INIT"

    def mark_order_paid(self) -> None:
        self.order_status = "PAID"

    def mark_order_cancelled(self) -> None:
        self.order_status = "CANCELLED"

    def mark_service_active(self, service_name: str) -> None:
        self.active_services.add(service_name)

    def is_service_active(self, service_name: str) -> bool:
        return service_name in self.active_services

    # ------------------------------------------------------------------
    # Pruner-запросы
    # ------------------------------------------------------------------

    def can_pay(self, price: int = 0) -> bool:
        """balance=10000 → всегда True для любой реалистичной цены."""
        return self.balance >= price

    def can_cancel(self) -> bool:
        return self.order_status == "PAID"

    def can_rebook(self) -> bool:
        return self.order_status in ("INIT", "PAID")

    def update_from_step_type(self, step_type: str) -> None:
        """Обновить order_status по типу шага (статусный автомат)."""
        if step_type == "book":
            self.mark_order_placed()
        elif step_type == "pay":
            self.mark_order_paid()
        elif step_type == "cancel":
            self.mark_order_cancelled()
        elif step_type == "rebook":
            self.order_status = "INIT"
