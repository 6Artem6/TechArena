"""Трансляция Finding → узлы и рёбра NetworkX графа.

Единая точка входа для всех адаптеров: ToolAdapter.analyze() → AnalysisResult
→ FindingIngester.ingest() → граф.

Матрица трансляции:
  CALLS_SERVICE → ребро CALLS_SERVICE  (src_svc → tgt_svc)
  PROXY_ROUTE   → ребро CALLS_SERVICE  с via_proxy=True
  SERVICE_DEF   → узел SERVICE         (если отсутствует)
  HTTP_ENDPOINT → узел ENDPOINT        + ребро HAS_ENDPOINT от SERVICE
  CALLS_DB      → узел INFRA(db)       + ребро CALLS_DB
  ASYNC_CALL    → узел INFRA(mq)       + ребро ASYNC_MESSAGE

Прочие kinds (CONFIG_VALUE, ENV_ACCESS, IMPORT, TECH_DEBT, ...) не создают
рёбер в графе и молча пропускаются — они хранятся в AnalysisResult адаптера.
"""
from __future__ import annotations

from pathlib import Path

import networkx as nx

import re

from ..adapters.tools.base import AnalysisResult, Finding, FindingKind
from ..core.node_id import NodeId, make_service_id
from ..core.walker import service_name_of
from .node_typing import is_library_service, node_attrs

_SPRING_DEFAULT_RE = re.compile(r"^\$\{[^:}]+:([^}]*)\}$")

# findings с confidence ниже порога не добавляются в граф
MIN_CONFIDENCE: float = 0.7


class FindingIngester:
    """Принимает AnalysisResult и добавляет рёбра/узлы в NetworkX граф.

    Инстанс привязан к одному графу и корню репозитория — не переиспользовать
    между разными build()-вызовами.
    """

    def __init__(self, graph: nx.DiGraph, repo_root: Path) -> None:
        self._g = graph
        self._root = repo_root
        self._ingested: int = 0
        self._skipped_low_conf: int = 0

    @property
    def stats(self) -> dict[str, int]:
        return {
            "ingested": self._ingested,
            "skipped_low_confidence": self._skipped_low_conf,
        }

    def ingest(self, result: AnalysisResult) -> int:
        """Загрузить все findings из result в граф.

        Returns:
            Количество добавленных узлов/рёбер.
        """
        added = 0
        for finding in result.findings:
            if finding.confidence < MIN_CONFIDENCE:
                self._skipped_low_conf += 1
                continue
            source_service = self._service_from_finding(finding)
            added += self._dispatch(finding, source_service)
        self._ingested += added
        return added

    # ─── Внутренние методы ────────────────────────────────────────────────────

    def _service_from_finding(self, finding: Finding) -> str:
        file_path = finding.location.file if finding.location else ""
        if not file_path:
            return "unknown"
        return service_name_of(Path(file_path), self._root)

    def _dispatch(self, f: Finding, source_service: str) -> int:
        try:
            kind = f.kind if isinstance(f.kind, FindingKind) else FindingKind(f.kind)
        except ValueError:
            return 0
        _handlers = {
            FindingKind.CALLS_SERVICE: self._ingest_calls_service,
            FindingKind.PROXY_ROUTE:   self._ingest_proxy_route,
            FindingKind.SERVICE_DEF:   self._ingest_service_def,
            FindingKind.HTTP_ENDPOINT: self._ingest_http_endpoint,
            FindingKind.CALLS_DB:      self._ingest_calls_db,
            FindingKind.ASYNC_CALL:    self._ingest_async_message,
        }
        handler = _handlers.get(kind)
        return handler(f, source_service) if handler else 0

    # ─── Обработчики по типу ──────────────────────────────────────────────────

    def _ingest_calls_service(self, f: Finding, source_service: str) -> int:
        # YamlAdapter depends_on: data содержит "caller"/"callee", не "service"
        caller = f.data.get("caller")
        callee = f.data.get("callee")
        if caller and callee:
            src_id = self._ensure_service(caller)
            tgt_id = self._ensure_service(callee)
        else:
            target = (
                f.data.get("service")
                or f.data.get("target")
                or f.data.get("host")
            )
            if not target:
                return 0
            src_id = self._ensure_service(source_service)
            tgt_id = self._ensure_service(target)

        if not self._g.has_edge(src_id, tgt_id):
            self._g.add_edge(
                src_id, tgt_id,
                edge_type="CALLS_SERVICE",
                confidence=f.confidence,
                source_tool=f.source_tool,
                file=f.location.file if f.location else "",
                line=f.location.line if f.location else 0,
            )
            return 1
        return 0

    def _ingest_proxy_route(self, f: Finding, source_service: str) -> int:
        # PROXY_ROUTE: data["service"] уже извлечено NginxAdapter из proxy_pass URL
        target = f.data.get("service") or f.data.get("upstream")
        if not target or target.startswith("$"):
            # переменная nginx ($backend) — цель не известна статически
            return 0
        src_id = self._ensure_service(source_service)
        tgt_id = self._ensure_service(target)
        if not self._g.has_edge(src_id, tgt_id):
            self._g.add_edge(
                src_id, tgt_id,
                edge_type="CALLS_SERVICE",
                via_proxy=True,
                confidence=f.confidence,
                source_tool=f.source_tool,
            )
            return 1
        return 0

    def _ingest_service_def(self, f: Finding, source_service: str) -> int:
        name = f.data.get("name") or source_service
        if not name:
            return 0
        node_id = make_service_id(name)
        if not self._g.has_node(node_id):
            nt = "LIBRARY" if is_library_service(name) else "SERVICE"
            extra = {k: v for k, v in f.data.items() if k != "name"}
            self._g.add_node(
                node_id,
                **node_attrs(nt, service=name, source_tool=f.source_tool, **extra),
            )
            return 1
        return 0

    def _ingest_http_endpoint(self, f: Finding, source_service: str) -> int:
        svc = f.data.get("service") or source_service
        path = f.data.get("path", "/")
        method = f.data.get("method", "*")
        ep_id = NodeId(f"{svc}::endpoint::{method}:{path}")
        if not self._g.has_node(ep_id):
            self._g.add_node(
                ep_id,
                **node_attrs("ENDPOINT", http_method=method, path=path,
                              service=svc, source_tool=f.source_tool),
            )
            svc_id = self._ensure_service(svc)
            self._g.add_edge(svc_id, ep_id, edge_type="HAS_ENDPOINT")
            return 1
        return 0

    def _ingest_calls_db(self, f: Finding, source_service: str) -> int:
        target = (
            f.data.get("host")
            or f.data.get("db_name")
            or (f.data.get("url") or "")[:40]
        )
        if not target:
            return 0
        src_id = self._ensure_service(source_service)
        db_id = NodeId(f"db::{target}")
        if not self._g.has_node(db_id):
            self._g.add_node(
                db_id,
                **node_attrs("INFRA", infra_type="db",
                              protocol=f.data.get("protocol", ""), host=target),
            )
        if not self._g.has_edge(src_id, db_id):
            self._g.add_edge(
                src_id, db_id,
                edge_type="CALLS_DB",
                confidence=f.confidence,
                source_tool=f.source_tool,
            )
            return 1
        return 0

    def _ingest_async_message(self, f: Finding, source_service: str) -> int:
        broker_host = f.data.get("host") or f.data.get("broker", "")
        if not broker_host:
            return 0
        broker_host = _resolve_spring_default(broker_host)
        # localhost / 127.0.0.1 — не внешний сервис, пропускаем как INFRA-only
        if broker_host in ("localhost", "127.0.0.1", ""):
            return 0
        src_id = self._ensure_service(source_service)
        mq_id = NodeId(f"mq::{broker_host}")
        if not self._g.has_node(mq_id):
            self._g.add_node(
                mq_id,
                **node_attrs("INFRA", infra_type="mq",
                              broker=f.data.get("broker", ""), host=broker_host),
            )
        if not self._g.has_edge(src_id, mq_id):
            self._g.add_edge(
                src_id, mq_id,
                edge_type="ASYNC_MESSAGE",
                confidence=f.confidence,
                source_tool=f.source_tool,
            )
            return 1
        return 0

    # ─── Утилиты ──────────────────────────────────────────────────────────────

    def _ensure_service(self, service: str) -> NodeId:
        node_id = make_service_id(service)
        if not self._g.has_node(node_id):
            nt = "LIBRARY" if is_library_service(service) else "SERVICE"
            self._g.add_node(node_id, **node_attrs(nt, service=service))
        return node_id


def _resolve_spring_default(value: str) -> str:
    """Извлечь default из Spring EL: `${key:default}` → `default`."""
    m = _SPRING_DEFAULT_RE.match(value)
    return m.group(1) if m else value
