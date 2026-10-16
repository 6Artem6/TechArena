"""BFS обход графа от изменённого узла.

change_spec задаёт точку входа: сервис, файл, класс или метод.
BFS обходит граф в обе стороны по всем типам рёбер с ограничением глубины.

Исключения из обхода:
  - USES_DTO backward=False: узлы ts-common не взрывают граф
  - CONTAINS не считается в depth budget (структурная связь, не зависимость)
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import networkx as nx

from ..core.node_id import NodeId, make_class_id, make_method_id, make_service_id

# Рёбра которые НЕ считаются в глубину (структурные и sibling-связи)
_NO_DEPTH_COST = frozenset({"CONTAINS", "SIBLING_OF"})

# Рёбра которые не обходятся в обратном направлении (чтобы не взрывать через DTO)
_NO_BACKWARD = frozenset({"USES_DTO"})


@dataclass
class ChangeSpec:
    """Точка входа для blast radius — что изменилось."""
    service: str
    file: str = ""       # путь к файлу
    class_fqn: str = ""  # package.ClassName
    method: str = ""     # имя метода


@dataclass
class BlastRadiusResult:
    change_spec: ChangeSpec
    affected_nodes: list[str]
    graph_stats: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    resolution_level: str = "exact_method"  # уровень разрешения стартового узла
    node_details: dict = field(default_factory=dict)  # node_id → enriched attrs


def compute_blast_radius(
    graph: nx.DiGraph,
    spec: ChangeSpec,
    max_depth: int = 6,
) -> BlastRadiusResult:
    """BFS от change_spec. Возвращает множество затронутых узлов.

    Обход двунаправленный: вперёд по CALLS_SERVICE и назад по CALLS_SERVICE
    (кто вызывает изменившийся сервис).

    CONTAINS рёбра обходятся без счёта глубины — чтобы попасть в класс/метод
    внутри затронутого сервиса.

    Fallback иерархия (task 20):
      exact_method → class → file_classes → service → not_found
    """
    start_nodes, level = _resolve_start(graph, spec)
    warnings: list[str] = []

    if not start_nodes:
        warnings.append(f"Начальный узел не найден в графе: {spec}")
        return BlastRadiusResult(
            change_spec=spec,
            affected_nodes=[],
            graph_stats=_graph_stats(graph),
            warnings=warnings,
            resolution_level="not_found",
        )

    if level != "exact_method":
        warnings.append(
            f"Fallback: разрешено как '{level}' вместо exact_method для {spec}"
        )

    visited: dict[str, int] = {}  # node_id → min depth достижения
    queue: deque[tuple[str, int]] = deque()

    for n in start_nodes:
        visited[str(n)] = 0
        queue.append((str(n), 0))

    while queue:
        node, depth = queue.popleft()

        if depth > max_depth:
            continue

        # Вперёд по CONTAINS и SIBLING_OF: разворачиваем классы/методы внутри
        # затронутого сервиса и переходим к сиблинг-сервисам без затрат глубины.
        # Вперёд по ASYNC_MESSAGE: SERVICE → mq::queue → consumer (с затратой глубины).
        # CALLS_SERVICE вперёд НЕ обходим — иначе взрывается весь граф.
        for _, neighbor, data in graph.out_edges(node, data=True):
            edge_type = data.get("edge_type", "")
            if edge_type in _NO_DEPTH_COST:
                if neighbor not in visited:
                    visited[neighbor] = depth  # cost=0 для CONTAINS и SIBLING_OF
                    queue.append((neighbor, depth))
            elif edge_type == "ASYNC_MESSAGE":
                next_depth = depth + 1
                if neighbor not in visited and next_depth <= max_depth:
                    visited[neighbor] = next_depth
                    queue.append((neighbor, next_depth))

        # Назад по CALLS_SERVICE и ASYNC_MESSAGE: кто зависит от нас — они затронуты
        # CONTAINS назад не обходим (SERVICE→CLASS структурная связь)
        for neighbor, _, data in graph.in_edges(node, data=True):
            edge_type = data.get("edge_type", "")
            if edge_type not in ("CALLS_SERVICE", "HAS_ENDPOINT", "ASYNC_MESSAGE"):
                continue
            next_depth = depth + 1
            if neighbor not in visited and next_depth <= max_depth:
                visited[neighbor] = next_depth
                queue.append((neighbor, next_depth))

    # Убрать стартовые узлы из результата (они — причина, не следствие)
    start_set = {str(s) for s in start_nodes}
    affected = [n for n in visited if n not in start_set]

    # Enriched metadata для affected узлов
    node_details: dict[str, dict] = {}
    _ENRICHED_KEYS = ("node_type", "arch_role", "criticality", "bfs_role",
                      "flow_steps", "logger_tags", "deprecated", "has_bug_note",
                      "arch_pattern", "listen_port")
    for n in affected:
        if graph.has_node(n):
            attrs = graph.nodes[n]
            detail: dict = {}
            for k in _ENRICHED_KEYS:
                v = attrs.get(k)
                if v is not None:
                    detail[k] = v
            if detail:
                node_details[n] = detail

    return BlastRadiusResult(
        change_spec=spec,
        affected_nodes=sorted(affected),
        graph_stats=_graph_stats(graph),
        warnings=warnings,
        resolution_level=level,
        node_details=node_details,
    )


def _resolve_start(graph: nx.DiGraph, spec: ChangeSpec) -> tuple[list[str], str]:
    """Найти стартовые узлы с fallback-иерархией (task 20).

    Иерархия (возвращает первый уровень где есть узлы):
      exact_method  — точный метод по service+class+method
      class         — класс по service+class_fqn
      file_classes  — все классы файла по spec.file
      service       — SERVICE-узел сервиса
      not_found     — ничего не нашли
    """
    # 1. Точный метод
    if spec.method and spec.class_fqn:
        n = str(make_method_id(spec.service, spec.class_fqn, spec.method))
        if graph.has_node(n):
            return [n], "exact_method"

    # 2. Точный класс
    if spec.class_fqn:
        n = str(make_class_id(spec.service, spec.class_fqn))
        if graph.has_node(n):
            return [n], "class"

    # 3. Все классы файла (когда задан путь к файлу)
    if spec.file:
        file_classes = [
            n for n, d in graph.nodes(data=True)
            if d.get("file") == spec.file and d.get("node_type") == "CLASS"
        ]
        if file_classes:
            return file_classes, "file_classes"

    # 4. Сервис
    svc = str(make_service_id(spec.service))
    if graph.has_node(svc):
        return [svc], "service"

    return [], "not_found"


def _graph_stats(graph: nx.DiGraph) -> dict:
    return {
        "total_nodes": graph.number_of_nodes(),
        "total_edges": graph.number_of_edges(),
    }
