"""JSON-сериализация результата blast radius.

Всё в stdout — чистый JSON.
Все логи/предупреждения — в stderr.
"""
import json
import sys
from dataclasses import asdict

from ..graph.bfs import BlastRadiusResult


def to_json(result: BlastRadiusResult, indent: int | None = None) -> str:
    payload = {
        "change_spec": {
            "service":   result.change_spec.service,
            "file":      result.change_spec.file,
            "class":     result.change_spec.class_fqn,
            "method":    result.change_spec.method,
        },
        "affected_nodes": result.affected_nodes,
        "graph_stats":    result.graph_stats,
    }
    if result.warnings:
        payload["warnings"] = result.warnings
    if result.resolution_level != "exact_method":
        payload["resolution_level"] = result.resolution_level
    if result.node_details:
        payload["node_details"] = result.node_details
    return json.dumps(payload, ensure_ascii=False, indent=indent)


def print_result(result: BlastRadiusResult, pretty: bool = False) -> None:
    """Вывести результат в stdout. Предупреждения — в stderr."""
    if result.warnings:
        for w in result.warnings:
            print(f"[WARN] {w}", file=sys.stderr)
    indent = 2 if pretty else None
    print(to_json(result, indent=indent))
