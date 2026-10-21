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

import re
import sys

from src._config import _ROOT as _HERE, cfg_get
sys.path.insert(0, str(_HERE))

from src.core.skeleton_index import Index  # noqa: E402
from src.localization.service_classifier import detect_infra_executes  # noqa: E402
from src.localization.searcher import _is_g_service, _is_other_service  # noqa: E402


# ---------------------------------------------------------------------------
# Авто-обнаружение Spring Security фильтров (P-57, P-101)
# ---------------------------------------------------------------------------

_SECURITY_FILTERS_CACHE: set[str] | None = None

def _get_security_filter_cfg() -> tuple[list[str], str, list[str], list[str]]:
    """Загрузить критерии Security-фильтра из domain_config.yaml."""
    cfg = cfg_get("security_filter", {})
    fqn_markers     = cfg.get("fqn_markers", [".jwt.", ".security.jwt."])
    method_prefix   = cfg.get("method_prefix", "doFilter")
    extends_markers = cfg.get("extends_markers", ["OncePerRequestFilter", "GenericFilterBean"])
    impl_markers    = cfg.get("implements_markers", ["javax.servlet.Filter", "jakarta.servlet.Filter"])
    return fqn_markers, method_prefix, extends_markers, impl_markers


def _detect_security_filters(index: Index) -> set[str]:
    """Найти Spring Security фильтры в скелете (ленивая инициализация).

    Два пути поиска (оба из domain_config.yaml → security_filter):
      1. Inheritance path: тип extends OncePerRequestFilter / GenericFilterBean
         или implements javax/jakarta.servlet.Filter → методы с method_prefix
      2. FQN-marker path (legacy): FQN содержит .jwt. — fallback для совместимости
         и для будущих фреймворков, где extends_raw не заполнен в скелете.

    Это поддерживает javax (Spring Boot 2) и jakarta (Spring Boot 3+) без хардкода.
    """
    global _SECURITY_FILTERS_CACHE
    if _SECURITY_FILTERS_CACHE is not None:
        return _SECURITY_FILTERS_CACHE

    fqn_markers, method_prefix, extends_markers, impl_markers = _get_security_filter_cfg()
    extends_set = set(extends_markers)
    impl_set    = set(impl_markers)

    # Path 1: найти типы через extends_raw / implements_raw
    filter_type_nids: set[str] = set()
    for nid, node in index.N.items():
        if node.get("kind") != "type":
            continue
        ext  = set(node.get("extends_raw") or [])
        impl = set(node.get("implements_raw") or [])
        if ext & extends_set or impl & impl_set:
            filter_type_nids.add(nid)

    filters: set[str] = set()

    # Методы найденных типов с нужным prefix
    for nid, node in index.N.items():
        if node.get("kind") != "method":
            continue
        name = node.get("name", "")
        if not name.startswith(method_prefix):
            continue
        if node.get("owner", "") in filter_type_nids:
            filters.add(nid)
            continue

        # Path 2: legacy FQN-marker (fallback)
        parts = nid.split(":", 2)
        if len(parts) < 3:
            continue
        fqn = parts[2]
        if any(marker in fqn for marker in fqn_markers):
            filters.add(nid)

    _SECURITY_FILTERS_CACHE = filters
    return filters


# ---------------------------------------------------------------------------
# ORM entity loading: data_access(table) → Entity#<init>()
#
# JPA/MongoDB при загрузке строк из БД вызывают no-arg конструктор entity.
# Статический скелет не знает об этом, но мы можем вывести связь из аннотации
# @Table(name="tableName") на entity-классах.
#
# Алгоритм:
#   1. Найти типы с @Table(name=X) → получить <init>() этого типа
#   2. Ключ: res:table:{module}.{tableName}
#   3. В BFS: когда встречаем data_access(method→table), добавляем <init>() в BFS
# ---------------------------------------------------------------------------

_TABLE_ENTITY_MAP_CACHE: "dict[str, list[str]] | None" = None
_TABLE_NAME_RE = re.compile(r'name\s*=\s*["\']([^"\']+)["\']')


def _build_table_entity_map(index: Index) -> dict[str, list[str]]:
    global _TABLE_ENTITY_MAP_CACHE
    if _TABLE_ENTITY_MAP_CACHE is not None:
        return _TABLE_ENTITY_MAP_CACHE

    result: dict[str, list[str]] = {}
    N = index.N

    # type_nid → [<init> method nids]
    type_to_inits: dict[str, list[str]] = {}
    for nid, node in N.items():
        if node.get("kind") != "method" or node.get("name") != "<init>":
            continue
        owner = node.get("owner", "")
        if owner:
            type_to_inits.setdefault(owner, []).append(nid)

    for type_nid, type_node in N.items():
        if type_node.get("kind") != "type":
            continue
        module = type_node.get("module", "")
        for ann in type_node.get("annotations", []):
            if not isinstance(ann, dict) or ann.get("name") != "Table":
                continue
            m = _TABLE_NAME_RE.search(ann.get("args", ""))
            if not m:
                continue
            table_key = f"res:table:{module}.{m.group(1)}"
            inits = type_to_inits.get(type_nid, [])
            # Keep only no-arg constructors (empty params)
            no_arg = [i for i in inits if N[i].get("descriptor", "") in ("()V", "") or not N[i].get("params_as_written")]
            if no_arg:
                result[table_key] = no_arg

    _TABLE_ENTITY_MAP_CACHE = result
    return result


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
                # Пропустить @ElementCollection таблицы (collection_of в schema) —
                # они добавляются через BFS от primary методов, не от DI-контрактов
                if "collection_of" in index.N.get(t, {}):
                    continue
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


_PURE_UTILITY_CACHE: frozenset[str] | None = None
_NON_ENTRY_MODULES_CACHE: frozenset[str] | None = None


def _get_non_entry_modules(index: Index) -> frozenset[str]:
    """Топологически: модули без HTTP entry points."""
    global _NON_ENTRY_MODULES_CACHE
    if _NON_ENTRY_MODULES_CACHE is None:
        handler_mods = frozenset(
            index.module.get(nid, "") for nid, _, _ in index.handlers
        ) - frozenset([""])
        all_mods = frozenset(index.module.values()) - frozenset([""])
        _NON_ENTRY_MODULES_CACHE = all_mods - handler_mods
    return _NON_ENTRY_MODULES_CACHE


def _get_pure_utility_modules(index: Index) -> frozenset[str]:
    """Топологически: pure utility libs — без HTTP handlers И без входящих remote_call.

    ts-common — типичный пример: shared DTOs/utils, не является REST-сервисом.
    Методы из таких модулей с 0 исходящих calls — shallow getters/enums, безопасно убрать.
    Более точный критерий чем _get_non_entry_modules: исключает сервисы без handlers
    но вызываемые по REST (например ts-gateway-service может вызываться).
    """
    global _PURE_UTILITY_CACHE
    if _PURE_UTILITY_CACHE is None:
        non_entry = _get_non_entry_modules(index)
        # Собрать модули которые являются targets remote_call
        remote_target_mods: set[str] = set()
        for targets in index.wiring.get("remote_call", {}).values():
            for t in targets:
                mod = index.module.get(t, "")
                if mod:
                    remote_target_mods.add(mod)
        _PURE_UTILITY_CACHE = non_entry - frozenset(remote_target_mods)
    return _PURE_UTILITY_CACHE


def _is_shallow_method(nid: str, index: Index, non_entry_mods: frozenset[str]) -> bool:
    """True если метод — shallow getter/enum/util из pure utility модуля.

    Топологический критерий (zero-hardcode):
      - принадлежит pure utility модулю (без handlers, без incoming remote_call)
      - не конструктор (<init>)
      - 0 исходящих calls (простой getter, enum accessor, utility)
    """
    if index.module.get(nid, "") not in non_entry_mods:
        return False
    node = index.N.get(nid, {})
    if node.get("kind") != "method":
        return False
    if node.get("name") == "<init>":
        return False
    return not index.calls.get(nid)


def pruned_closure(
    entries: set[str],
    index: Index,
    train_type: str = "",
    excluded_modules: frozenset[str] = frozenset(),
) -> tuple[set, set, set, set]:
    """BFS closure с Oracle-фильтрацией remote_call по типу поезда.

    Точная копия baseline Index.closure(), но при наличии train_type
    пропускает remote_call к сиблинг-сервисам неверного типа:
      train_type="G" → пропустить remote к OTHER-сервисам
      train_type="D" → пропустить remote к G-сервисам
    excluded_modules: пропустить remote_call в эти сервисы (опциональные сервисы,
    которые не были заказаны в данном сценарии, например ts-food-service).
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
            if excluded_modules and target_mod in excluded_modules:
                continue
            remote.add((m, t))
            st.append((t, index.unit_of(t)))
        for t in index.wiring["data_access"].get(m, set()):
            data.add((m, t))
            # ORM entity loading: repository.find*() → Entity#<init>()
            for init_nid in _build_table_entity_map(index).get(t, ()):
                if (init_nid, u) not in seen:
                    st.append((init_nid, u))

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

    # ORM entity loading: для каждой data_access(table) → BFS через Entity#<init>()
    tbl_map = _build_table_entity_map(index)
    orm_queue = []
    for _src, tbl in data:
        for init_nid in tbl_map.get(tbl, ()):
            if init_nid not in methods:
                orm_queue.append(init_nid)
    if orm_queue:
        # Mini-BFS внутри сервиса для каждого entity конструктора
        orm_seen: set[tuple[str, str]] = set()
        orm_st = [(nid, index.unit_of(nid)) for nid in orm_queue if nid in index.module]
        while orm_st:
            m, u = orm_st.pop()
            if (m, u) in orm_seen:
                continue
            orm_seen.add((m, u))
            methods.add(m)
            for t in index.calls.get(m, ()):
                for x in ({t} | index.cha(t)):
                    if not index.N[x].get("abstract") and u in index.units.get(index.module.get(x), ()):
                        edges.add((m, x))
                        orm_st.append((x, u))

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
