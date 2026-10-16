"""Типизация узлов графа: arch_role, bfs_role, criticality, node_type (ADR-002, ADR-004, ADR-020).

resolve_node_type() — правильный node_type вместо всегда "CLASS":
  interface  → INTERFACE
  abstract   → ABSTRACT_CLASS
  @Repository / extends *Repository → REPOSITORY
  иначе      → CLASS

arch_role_from_package() — роль из пути пакета (ADR-004 ROLE_MAP):
  controller/ → http_handler
  service/    → business_logic
  repository/ → data_access
  entity/     → data_model
  dto/        → contract
  config/     → configuration

bfs_role() — anchor vs active (ADR-020):
  anchor: SERVICE, INTERFACE, LIBRARY, DTO
  active: CLASS, ABSTRACT_CLASS, REPOSITORY, METHOD, FIELD, ENDPOINT

base_criticality() — по типу узла (ADR-020):
  SERVICE / INFRA   → 1.0
  INTERFACE         → 0.9
  ABSTRACT_CLASS    → 0.75
  CLASS / REPOSITORY → 0.7
  METHOD            → 0.6
  ENDPOINT          → 0.65
  CONFIG_SECTION    → 0.8
  UnknownDynamic    → 1.5
"""
from __future__ import annotations

# ─── ROLE_MAP (ADR-004) ───────────────────────────────────────────────────────

_ROLE_MAP: dict[str, str] = {
    "controller": "http_handler",
    "controllers": "http_handler",
    "rest": "http_handler",
    "web": "http_handler",
    "service": "business_logic",
    "services": "business_logic",
    "usecase": "business_logic",
    "impl": "business_logic",
    "repository": "data_access",
    "repositories": "data_access",
    "dao": "data_access",
    "persistence": "data_access",
    "entity": "data_model",
    "entities": "data_model",
    "model": "data_model",
    "models": "data_model",
    "dto": "contract",
    "dtos": "contract",
    "request": "contract",
    "response": "contract",
    "config": "configuration",
    "configuration": "configuration",
    "util": "utility",
    "utils": "utility",
    "helper": "utility",
    "exception": "error_handling",
    "error": "error_handling",
    "security": "security",
    "auth": "security",
    "event": "event",
    "listener": "event",
    "publisher": "event",
    "mq": "event",
    "port": "port",
    "adapter": "adapter",
    "domain": "domain",
    "application": "application_layer",
    "infrastructure": "infrastructure",
}

# Суффиксы классов-репозиториев (Spring Data)
_REPOSITORY_SUPERCLASSES = frozenset({
    "CrudRepository", "JpaRepository", "MongoRepository",
    "PagingAndSortingRepository", "ReactiveCrudRepository",
    "ReactiveMongoRepository",
})

# Аннотации-маркеры
_REPOSITORY_ANNOTATIONS = frozenset({"@Repository"})
_LIBRARY_SERVICES = frozenset({
    "ts-common", "edu.fudan.common", "fdse.microservice", "org.myproject.ms.monitoring",
})

# ─── Критичность по типу узла (ADR-020) ──────────────────────────────────────

_CRITICALITY: dict[str, float] = {
    "SERVICE":              1.0,
    "LIBRARY":              0.9,
    "INTERFACE":            0.9,
    "ABSTRACT_CLASS":       0.75,
    "REPOSITORY":           0.7,
    "CLASS":                0.7,
    "METHOD":               0.6,
    "ENDPOINT":             0.65,
    "CONFIG_SECTION":       0.8,
    "INFRA":                1.0,
    "UnknownDynamicTarget": 1.5,
}

# ─── BFS роль (ADR-020) ───────────────────────────────────────────────────────

_BFS_ROLE: dict[str, str] = {
    "SERVICE":              "anchor",
    "LIBRARY":              "anchor",
    "INTERFACE":            "anchor",
    "CLASS":                "active",
    "ABSTRACT_CLASS":       "active",
    "REPOSITORY":           "active",
    "METHOD":               "active",
    "ENDPOINT":             "active",
    "INFRA":                "anchor",
    "CONFIG_SECTION":       "active",
    "UnknownDynamicTarget": "active",
}


# ─── Публичный API ────────────────────────────────────────────────────────────

def resolve_node_type(
    kind: str,
    annotations: list[str],
    extends_class: str,
    is_abstract: bool,
    implements_interfaces: list[str] | None = None,
) -> str:
    """Преобразовать Java class-kind + аннотации в правильный node_type (ADR-002).

    Args:
        kind:                  "class" | "interface" | "enum"
        annotations:           список аннотаций класса
        extends_class:         суперкласс (class) или родительский интерфейс (interface)
        is_abstract:           True если abstract modifier
        implements_interfaces: список implements (class) или extends (interface)
    """
    if kind == "enum":
        return "CLASS"

    # Репозиторий: ДО interface/class — Spring репозитории объявлены как
    # `interface FooRepository extends CrudRepository<...>`
    ann_set = {a.lstrip("@").split("(")[0] for a in annotations}
    if "Repository" in ann_set:
        return "REPOSITORY"
    # Проверяем и extends_class, и каждый из implements_interfaces
    all_parents = ([extends_class] if extends_class else []) + (implements_interfaces or [])
    for parent in all_parents:
        if parent in _REPOSITORY_SUPERCLASSES:
            return "REPOSITORY"

    if kind == "interface":
        return "INTERFACE"

    if is_abstract:
        return "ABSTRACT_CLASS"

    return "CLASS"


def arch_role_from_package(fqn: str) -> str:
    """Определить роль класса из имени пакета (ADR-004).

    Берёт компоненты FQN и ищет ближайший в ROLE_MAP.
    Пример: "travel.controller.TravelController" → "http_handler"
    """
    parts = fqn.lower().split(".")
    # Обходим от конкретного к общему: ClassName → package → root
    for part in reversed(parts[:-1]):  # без имени класса
        role = _ROLE_MAP.get(part)
        if role:
            return role
    return "unknown"


def is_library_service(service_name: str) -> bool:
    """Проверить является ли сервис библиотекой (LIBRARY тип)."""
    return service_name in _LIBRARY_SERVICES or any(
        service_name.startswith(lib) for lib in _LIBRARY_SERVICES
    )


def base_criticality(node_type: str) -> float:
    """Базовая критичность узла по типу (ADR-020)."""
    return _CRITICALITY.get(node_type, 0.6)


def bfs_role(node_type: str) -> str:
    """BFS-роль узла: anchor (структурный) или active (точка изменений) (ADR-020)."""
    return _BFS_ROLE.get(node_type, "active")


def node_attrs(node_type: str, **extra) -> dict:
    """Сформировать словарь атрибутов узла с обязательными типизированными полями."""
    return {
        "node_type": node_type,
        "bfs_role": bfs_role(node_type),
        "criticality": base_criticality(node_type),
        **extra,
    }


def detect_arch_pattern(arch_roles: dict[str, int]) -> str:
    """Определить архитектурный паттерн сервиса по набору arch_role его узлов (ADR-004).

    Правила (применяются по приоритету):
      Hexagonal: есть port + adapter
      CQRS:      есть и command, и query ролей набор (ищем по именам пакетов)
      EDA:       event/publisher/listener преобладают над http_handler
      MVC:       http_handler + business_logic + data_access одновременно
      FLAT:      только http_handler без data_access (микролит без слоёв)
      UNKNOWN:   ни одно правило не сработало
    """
    roles = set(arch_roles)
    if "port" in roles and "adapter" in roles:
        return "HEXAGONAL"
    if "http_handler" in roles and "business_logic" in roles and "data_access" in roles:
        return "MVC"
    event_count = sum(arch_roles.get(r, 0) for r in ("event", "event_handling"))
    handler_count = arch_roles.get("http_handler", 0)
    if event_count > 0 and event_count >= handler_count:
        return "EDA"
    if "http_handler" in roles and "data_access" not in roles and "business_logic" not in roles:
        return "FLAT"
    if "business_logic" in roles:
        return "MVC"
    return "UNKNOWN"
