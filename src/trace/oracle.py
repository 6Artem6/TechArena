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
# StepRecord — graph-derived контекст одного шага
# ---------------------------------------------------------------------------

@dataclass
class StepRecord:
    """Состояние шага для graph-guided session history."""
    step_idx: int
    step_type: str
    found_handlers: list  # list[str] — nids
    reachable_nids: set   # set[str] — прямые remote_call-цели
    reachable_modules: set  # set[str] — module names reachable nids
    handler_names: list = field(default_factory=list)  # list[str] — "short-mod.methodName"


# ---------------------------------------------------------------------------
# Паттерны типа поезда (из domain_config.yaml → oracle_train_patterns)
#
# ВНИМАНИЕ: DongChe трактуется как D-тип (для точного pruning по BFS ветвям).
# В step_typer.py DongChe в G-диапазоне (разные роли!).
# NOT_G имеет приоритет над G: предотвращает false positive "not high-speed".
# ---------------------------------------------------------------------------

def _build_re(key: str) -> re.Pattern[str]:
    """Собрать regex из конфига (oracle_train_patterns[key]). Ошибка если ключа нет."""
    raw = cfg_get("oracle_train_patterns", {})
    alts = raw.get(key)
    if not alts:
        raise ValueError(f"oracle_train_patterns.{key} не найден в domain_config.yaml")
    return re.compile(r"\b(" + "|".join(alts) + r")\b", re.I)


def _build_not_g_re() -> re.Pattern[str]:
    raw = cfg_get("oracle_train_patterns", {})
    alts = raw.get("NOT_G")
    if not alts:
        raise ValueError("oracle_train_patterns.NOT_G не найден в domain_config.yaml")
    return re.compile("|".join(alts), re.I)


_TRAIN_G     = _build_re("G")
_TRAIN_D     = _build_re("D")
_TRAIN_NOT_G = _build_not_g_re()

_SEAT_1ST = re.compile(r"\b(first.?class|business.?class|1st.?class)\b", re.I)
_SEAT_2ND = re.compile(r"\b(second.?class|economy|2nd.?class|standard)\b", re.I)
_HAS_FOOD = re.compile(
    r"\b(with\s+food|order\s+food|food\s+service|station\s+food|food\s+from\s+a\s+station)\b", re.I)
_NO_FOOD  = re.compile(r"\b(no\s+food|without\s+food)\b", re.I)
_HAS_INS  = re.compile(r"\bwith\b.{0,80}\b(assurance|insurance)\b", re.I)
_NO_INS   = re.compile(r"\b(without|no)\b.{0,80}\b(assurance|insurance)\b", re.I)
_HAS_LUG  = re.compile(r"\b(with\s+luggage|luggage\s+service|a\s+luggage\s+consignment)\b", re.I)
_NO_LUG   = re.compile(r"\b(no\s+luggage|without\s+luggage)\b", re.I)


# ---------------------------------------------------------------------------
# Oracle dataclass
# ---------------------------------------------------------------------------

@dataclass
class Oracle:
    account:     str  = ""
    balance:     int  = 10_000
    system_date: date = field(default_factory=date.today)
    trip_date:   date = field(default_factory=date.today)
    train_type:  str  = ""        # "G" | "D" | ""
    seat_class:  str  = ""        # "FIRST" | "SECOND" | ""
    order_status: str = ""        # "INIT" | "PAID" | "CANCELLED" | ""
    has_food:      bool = False
    has_insurance: bool = False
    has_luggage:   bool = False
    active_services: set = field(default_factory=set)
    step_history: list = field(default_factory=list)  # list[StepRecord]

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
        if _HAS_INS.search(text) and not _NO_INS.search(text):
            self.has_insurance = True
        if _HAS_LUG.search(text) and not _NO_LUG.search(text):
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

    def mark_order_collected(self) -> None:
        self.order_status = "COLLECTED"

    def mark_order_executed(self) -> None:
        self.order_status = "EXECUTED"

    def mark_service_active(self, service_name: str) -> None:
        self.active_services.add(service_name)

    def record_step(self, step_idx: int, step_type: str,
                    found_handlers, wiring: dict, module_map: dict,
                    calls_map: dict | None = None,
                    handler_names: "list[str] | None" = None) -> None:
        """Записать шаг в историю с graph-derived remote_call-соседями.

        Module-level: берём все remote_call-рёбра из модулей найденных handler-ов.
        Это обходит разрыв controller→interface→impl в skeleton-графе:
        remote_call-рёбра находятся на уровне impl-методов того же модуля.
        calls_map сохраняется для будущего использования.
        """
        found_list = list(found_handlers)
        reachable_nids: set = set()
        reachable_modules: set = set()

        rc_wiring = wiring.get("remote_call", {})
        found_modules = {module_map.get(nid, "") for nid in found_list} - {""}

        # Все remote_call из модулей найденных handler-ов
        for src_nid, tgt_nids in rc_wiring.items():
            src_mod = module_map.get(src_nid, "")
            if src_mod not in found_modules:
                continue
            for tgt_nid in tgt_nids:
                reachable_nids.add(tgt_nid)
                m = module_map.get(tgt_nid, "")
                if m:
                    reachable_modules.add(m)

        self.step_history.append(StepRecord(
            step_idx=step_idx,
            step_type=step_type,
            found_handlers=found_list,
            reachable_nids=reachable_nids,
            reachable_modules=reachable_modules,
            handler_names=list(handler_names) if handler_names else [],
        ))

    def is_service_active(self, service_name: str) -> bool:
        """True если сервис был активен в этом сценарии (полная история)."""
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
        elif step_type == "collect":
            self.mark_order_collected()
