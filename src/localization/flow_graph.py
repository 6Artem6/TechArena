"""Zero-hardcode flow tracking: branch consistency via skeleton graph topology.

Принцип: вместо парсинга текста ("high-speed" → train_type=G) — наблюдение
за тем, какая ветка сиблинг-пары была активирована на предыдущем шаге.

Алгоритм (только граф, никаких доменных словарей):

1. После каждого шага фиксируем entry_handlers → hs.
2. Для каждой сиблинг-пары (A, B) в hs определяем, кто из них выбран.
3. Фиксируем direct remote_call-мишени entry_handlers (из wiring).
4. Фиксируем domain-типы параметров выбранных handlers.

Внутренние/внешние типы определяются из самого скелета — нет ручных списков.
Только 'other_variant' и 'numeric_variant' пары считаются взаимоисключающими.
"""
from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core.skeleton_index import Index


def _build_internal_types(ix: "Index") -> frozenset[str]:
    """Простые имена типов, определённых внутри проекта (type-узлы скелета).

    Domain types (Order, TripInfo, Contacts…) — есть в скелете.
    Infrastructure types (HttpHeaders, String…) — нет.
    Никакого ручного списка — только граф.
    """
    result: set[str] = set()
    for v in ix.N.values():
        if v.get("kind") == "type" and v.get("fqn"):
            result.add(v["fqn"].split(".")[-1])
    return frozenset(result)


def _param_simple(p: str) -> str:
    """Нормализовать имя типа к простому имени без пакета и дженериков.

    'edu.fudan.common.entity.TravelInfo' → 'TravelInfo'
    'List<String>' → 'List'
    """
    return p.split(".")[-1].split("<")[0].strip()


_EXCLUSIVE_KINDS = frozenset(["other_variant", "numeric_variant"])


def _build_sibling_map(services: set[str]) -> dict[str, str]:
    """service → partner service.

    Только 'other_variant' и 'numeric_variant' — взаимоисключающие G/OTHER пары.
    'func_variant' и 'admin_variant' оба могут быть активны в одном сценарии.
    """
    from ..graph.siblings import detect_all_siblings  # noqa: PLC0415
    pairs = detect_all_siblings(services)
    result: dict[str, str] = {}
    for svcA, svcB, _score, kind in pairs:
        if kind in _EXCLUSIVE_KINDS:
            result[svcA] = svcB
            result[svcB] = svcA
    return result


class FlowContext:
    """Сессионный контекст шагов, основанный исключительно на топологии графа.

    Параллелен существующему train_type/oracle механизму — не заменяет, а дополняет.
    Активируется через `flow_ctx` параметр enhanced_ground().
    """

    def __init__(self, ix: "Index") -> None:
        self._sibling: dict[str, str] = _build_sibling_map(set(ix.module.values()))
        self._internal_types: frozenset[str] = _build_internal_types(ix)
        # frozenset({svcA, svcB}) → выбранный svc на этом сценарии
        self._chosen: dict[frozenset, str] = {}
        # Сервисы, достигнутые через direct remote_call из entry handlers
        self._remote_reached: Counter[str] = Counter()
        # Domain param-типы из entry handlers предыдущих шагов
        self._active_types: Counter[str] = Counter()

    # ------------------------------------------------------------------

    def _is_domain_type(self, p: str) -> bool:
        """True если тип является внутренним domain-классом проекта."""
        return _param_simple(p) in self._internal_types

    def record(self, entry_handlers: set[str], ix: "Index") -> None:
        """Зафиксировать состояние после выбора entry_handlers для шага.

        Вызывать ПОСЛЕ disambiguation (hs уже финальный для шага).
        """
        active_svcs = {ix.module.get(h, "") for h in entry_handlers if h in ix.module}
        active_svcs.discard("")

        # 1. Зафиксировать выбор ветки сиблингов
        for svc in active_svcs:
            partner = self._sibling.get(svc)
            if partner and partner not in active_svcs:
                key = frozenset([svc, partner])
                if key not in self._chosen:
                    self._chosen[key] = svc

        # 2. Direct remote_call мишени entry handlers
        rc_wiring = ix.wiring.get("remote_call", {})
        for h in entry_handlers:
            for tgt in rc_wiring.get(h, ()):
                tgt_svc = ix.module.get(tgt, "")
                if tgt_svc:
                    self._remote_reached[tgt_svc] += 1

        # 3. Domain param-типы — определяются из скелета, не из ручного списка
        for h in entry_handlers:
            n = ix.N.get(h, {})
            for p in (n.get("params_as_written") or []):
                if self._is_domain_type(p):
                    self._active_types[_param_simple(p)] += 1

    # ------------------------------------------------------------------

    def boost(self, handler_id: str, ix: "Index") -> float:
        """Аддитивный граф-буст для кандидата.

        Вызывать перед disambiguation, после BM25+BGE-M3.

        Только отрицательный штраф для неверной ветки сиблингов:
        положительные бусты не применяются, чтобы не поднимать best score
        и не сужать tau-окно для остальных кандидатов.
        """
        n = ix.N.get(handler_id, {})
        svc = n.get("module", "")
        if not svc:
            return 0.0

        partner = self._sibling.get(svc)
        if partner:
            key = frozenset([svc, partner])
            chosen = self._chosen.get(key)
            if chosen == partner:
                return -0.8

        return 0.0
