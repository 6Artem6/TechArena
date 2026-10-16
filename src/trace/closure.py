"""Фаза 3 — Улучшенный BFS closure.

enhanced_closure() — обёртка над baseline_closure.Index.closure():
  1. Добавляет Spring Security фильтры в executes каждого шага (P-101)
     — авто-обнаружение через пакет *.jwt.* + имя doFilter* (P-57)
  2. Добавляет DI-цепочку аутентификации (P-57): loadUserByUsername +
     UserDetails callbacks — через CHA, без хардкода классов
  3. Фильтрует infra-сервисы из executes (Token Taxonomy, P-93)
  4. Точка расширения для Oracle + Pruner (Фаза 3.2–3.3)
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_HERE))

from src.core.skeleton_index import Index  # noqa: E402
from src.localization.service_classifier import detect_infra_executes  # noqa: E402
from src.localization.searcher import _is_g_service, _is_other_service  # noqa: E402


# ---------------------------------------------------------------------------
# Авто-обнаружение Spring Security фильтров (P-57, P-101)
# ---------------------------------------------------------------------------

_SECURITY_FILTERS_CACHE: set[str] | None = None


def _detect_security_filters(index: Index) -> set[str]:
    """Найти Spring Security фильтры в скелете (ленивая инициализация).

    Ищет методы вида:
      kind=method + пакет содержит '.jwt.' + имя начинается с 'doFilter'

    Это заменяет хардкод NodeId JWTFilter (P-57).
    """
    global _SECURITY_FILTERS_CACHE
    if _SECURITY_FILTERS_CACHE is not None:
        return _SECURITY_FILTERS_CACHE

    filters: set[str] = set()
    for nid, node in index.N.items():
        if node.get("kind") != "method":
            continue
        name = node.get("name", "")
        if not name.startswith("doFilter"):
            continue
        # NodeId формат: "method:{module}:{fqn}#methodName(...)"
        parts = nid.split(":", 2)
        if len(parts) < 3:
            continue
        fqn = parts[2]  # "edu.fudan.common.security.jwt.JWTFilter#doFilter..."
        if ".jwt." in fqn or ".security.jwt." in fqn:
            filters.add(nid)

    _SECURITY_FILTERS_CACHE = filters
    return filters


# ---------------------------------------------------------------------------
# Auto-discovery DI-контрактов из скелета (P-57 расширение)
#
# Заменяет security_di из domain_config.yaml.
# Работает для любого web-фреймворка без знания Spring Security.
#
# Алгоритм:
#   1. Найти типы с ВНЕШНИМИ интерфейсами (implements_raw не в проекте)
#   2. Их @Override методы без статических входящих вызовов = DI-контракты
#   3. Исключить инициализационные методы (run) и Object-методы
#
# Классификация (для module-scoped применения):
#   unconditional — модуль никогда не вызывается статически из других модулей
#                   (фреймворк обращается напрямую через DI, напр. Spring Security)
#   conditional   — модуль вызывается статически, но есть DI-пробелы
#                   (напр. Callable.call() через executor)
# ---------------------------------------------------------------------------

_DI_CONTRACTS_CACHE: "tuple[dict, dict] | None" = None


def _discover_di_contracts(
    index: Index,
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Авто-найти DI-контракты из скелета. Возвращает (unconditional, conditional).

    unconditional: {module → {nid}} — добавлять в каждый сценарий
    conditional:   {module → {nid}} — добавлять только когда модуль уже в BFS
    """
    global _DI_CONTRACTS_CACHE
    if _DI_CONTRACTS_CACHE is not None:
        return _DI_CONTRACTS_CACHE

    N = index.N

    # Простые имена всех внутренних типов
    internal_simple: set[str] = {
        v.get("fqn", "").split(".")[-1]
        for v in N.values()
        if v.get("kind") == "type"
    }

    # Типы, реализующие интерфейсы НЕ из проекта (external)
    external_impl: set[str] = set()
    for nid, v in N.items():
        if v.get("kind") != "type":
            continue
        ext = [
            i
            for i in v.get("implements_raw", [])
            if i.split("<")[0].strip() not in internal_simple
        ]
        if ext:
            external_impl.add(nid)

    # Методы с входящими статическими вызовами
    called: set[str] = set()
    for targets in index.calls.values():
        called.update(targets)

    # Модули, хотя бы один метод которых виден в статическом графе
    called_modules: set[str] = {
        index.module.get(nid, "") for nid in called if nid
    }

    _SKIP = frozenset([
        "run",  # CommandLineRunner.run() — инициализация при старте, не трейс запроса
        "toString", "equals", "hashCode", "compareTo", "clone", "finalize",
    ])

    contracts: dict[str, set[str]] = {}
    for nid, v in N.items():
        if v.get("kind") != "method":
            continue
        if v.get("owner", "") not in external_impl:
            continue
        name = v.get("name", "")
        if name in _SKIP:
            continue
        anns = [a["name"] for a in v.get("annotations", [])]
        if "Override" not in anns:
            continue
        if v.get("abstract", False):
            continue
        mod = index.module.get(nid, "")
        if mod:
            contracts.setdefault(mod, set()).add(nid)

    # Классифицировать: unconditional (модуль невидим статически) vs conditional
    unconditional: dict[str, set[str]] = {}
    conditional: dict[str, set[str]] = {}
    for mod, nids in contracts.items():
        if mod not in called_modules:
            unconditional[mod] = nids
        else:
            conditional[mod] = nids

    _DI_CONTRACTS_CACHE = (unconditional, conditional)
    return unconditional, conditional


def _apply_di_contracts(
    methods: set[str],
    data: set[tuple],
    index: Index,
) -> None:
    """Добавить DI-контракты в methods и data (in-place).

    unconditional добавляется всегда (фреймворк обращается напрямую).
    conditional добавляется только если модуль уже в methods (BFS-достижимость).
    """
    unconditional, conditional = _discover_di_contracts(index)

    def _add(mod_nids: set[str]) -> None:
        methods.update(mod_nids)
        for nid in mod_nids:
            for t in index.wiring["data_access"].get(nid, set()):
                data.add((nid, t))

    for mod_nids in unconditional.values():
        _add(mod_nids)

    reached = {index.module.get(m, "") for m in methods}
    for mod, mod_nids in conditional.items():
        if mod in reached:
            _add(mod_nids)


# ---------------------------------------------------------------------------
# Инфра-сервисы которые НЕ должны попадать в executes (Token Taxonomy P-93)
# ---------------------------------------------------------------------------

_EXECUTES_INFRA_CACHE: frozenset[str] | None = None


def _get_executes_infra(index: Index) -> frozenset[str]:
    """Вернуть инфра-модули для фильтрации executes (ленивая инициализация)."""
    global _EXECUTES_INFRA_CACHE
    if _EXECUTES_INFRA_CACHE is not None:
        return _EXECUTES_INFRA_CACHE
    modules = set(index.module.values())
    _EXECUTES_INFRA_CACHE = detect_infra_executes(modules)
    return _EXECUTES_INFRA_CACHE


def pruned_closure(
    entries: set[str],
    index: Index,
    train_type: str = "",
) -> tuple[set, set, set, set]:
    """BFS closure с Oracle-фильтрацией remote_call по типу поезда.

    Точная копия baseline Index.closure(), но при наличии train_type
    пропускает remote_call к сиблинг-сервисам неверного типа:
      train_type="G" → пропустить remote к OTHER-сервисам
      train_type="D" → пропустить remote к G-сервисам
    """
    seen, edges, remote, data = set(), set(), set(), set()
    st = [(m, index.unit_of(m)) for m in entries if m in index.module]
    while st:
        m, u = st.pop()
        if (m, u) in seen:
            continue
        seen.add((m, u))
        for t in index.calls.get(m, ()):
            for x in ({t} | index.cha(t) | index.wiring["runtime_dispatch"].get(m, set())):
                if not index.N[x].get("abstract") and u in index.units.get(index.module.get(x), ()):
                    edges.add((m, x))
                    st.append((x, u))
        for t in (index.wiring["remote_call"].get(m, set()) |
                  index.wiring["message_delivery"].get(m, set())):
            target_mod = index.module.get(t, "")
            if train_type == "G" and _is_other_service(target_mod):
                continue
            if train_type == "D" and _is_g_service(target_mod):
                continue
            remote.add((m, t))
            st.append((t, index.unit_of(t)))
        for t in index.wiring["data_access"].get(m, set()):
            data.add((m, t))

    methods = {m for m, _ in seen}

    # P-101/P-57: Spring Security фильтры (JWT doFilter*)
    for flt in _detect_security_filters(index):
        methods.add(flt)

    # P-57: DI-контракты (авто-обнаружение для любого web-фреймворка)
    _apply_di_contracts(methods, data, index)

    infra_exec = _get_executes_infra(index)

    def _is_infra(nid: str) -> bool:
        return index.module.get(nid, "") in infra_exec

    def _is_exc_ctor(nid: str) -> bool:
        """True если метод — конструктор класса-исключения (Exception/Error в имени)."""
        node = index.N.get(nid, {})
        if node.get("name") != "<init>":
            return False
        owner_id = node.get("owner", "")
        owner_node = index.N.get(owner_id, {})
        simple = (owner_node.get("fqn", "") or "").split(".")[-1]
        return "Exception" in simple or "Error" in simple

    methods = {m for m in methods if not _is_infra(m) and not _is_exc_ctor(m)}
    edges   = {(s, t) for s, t in edges if not _is_infra(s) and not _is_infra(t)
               and not _is_exc_ctor(s) and not _is_exc_ctor(t)}
    remote  = {(s, t) for s, t in remote if not _is_infra(t)}

    return methods, edges, remote, data


def enhanced_closure(
    entries: set[str],
    index: Index,
) -> tuple[set, set, set, set]:
    """Расширенный BFS closure.

    Возвращает (methods, calls, remote, data) как baseline closure(),
    но с добавлением Spring Security фильтров (P-101) и фильтрацией
    инфра-методов из ВСЕХ типов рёбер (P-93).

    Примечание: baseline closure() добавляет remote target в BFS —
    поэтому infra-сервисы полностью обходятся. Нужно фильтровать постфактум.
    """
    methods, edges, remote, data = index.closure(entries)

    # P-101/P-57: Spring Security фильтры (JWT doFilter*)
    for flt in _detect_security_filters(index):
        methods.add(flt)

    # P-57: DI-контракты (авто-обнаружение для любого web-фреймворка)
    _apply_di_contracts(methods, data, index)

    # P-93: Убрать инфра-сервисы из ВСЕХ типов рёбер
    # Baseline closure() переходит через remote_call в infra-сервисы, обходит их,
    # и добавляет все их calls в edges. Фильтруем постфактум.
    infra_exec = _get_executes_infra(index)

    def _is_infra(node_id: str) -> bool:
        return index.module.get(node_id, "") in infra_exec

    methods = {m for m in methods if not _is_infra(m)}
    edges   = {(s, t) for s, t in edges if not _is_infra(s) and not _is_infra(t)}
    remote  = {(s, t) for s, t in remote if not _is_infra(t)}

    return methods, edges, remote, data
