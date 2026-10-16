"""Построение NetworkX-графа из результатов парсинга.

Типы узлов:
  SERVICE   — сервис целиком
  CLASS     — Java-класс / интерфейс / enum
  METHOD    — метод класса
  ENDPOINT  — HTTP-endpoint (путь + метод)
  INFRA     — инфраструктурный узел (db::mysql, mq::rabbitmq)

Типы рёбер:
  CONTAINS      — SERVICE→CLASS, CLASS→METHOD, CLASS→ENDPOINT
  CALLS_SERVICE — SERVICE/CLASS/METHOD → SERVICE (межсервисный вызов)
  HAS_ENDPOINT  — SERVICE/CLASS → ENDPOINT
  CALLS_DB      — SERVICE → INFRA(db)
  ASYNC_MESSAGE — SERVICE → INFRA(mq)
  USES_DTO      — SERVICE → SERVICE (импорт DTO-классов из другого сервиса)

Источники данных:
  1. LanguageParser (JavaParser, PythonParser, GoParser) — AST + regex
     Каждый парсер реализует один протокол; диспатч по языку.
  2. ToolRegistry — конфиг-адаптеры (YamlAdapter, NginxAdapter, ...)
     Все findings попадают в граф через FindingIngester.
"""
from __future__ import annotations

from pathlib import Path
import networkx as nx

from ..adapters.tools.base import AnalysisOptions
from ..adapters.tools.registry import ToolRegistry, detect_config_type
from ..core.node_id import make_class_id, make_method_id, make_service_id, NodeId
from ..core.source_pipeline import default_registry
from ..core.walker import walk, service_name_of
from ..parsers.base import LanguageParser
from ..parsers.go import GoParser
from ..parsers.java import JavaParser, ParsedJavaFile
from ..parsers.python import PythonParser
from .ingester import FindingIngester
from .node_typing import (
    arch_role_from_package, detect_arch_pattern, is_library_service,
    node_attrs, resolve_node_type, _REPOSITORY_SUPERCLASSES,
)

_REPOSITORY_SUPERCLASSES_SET = _REPOSITORY_SUPERCLASSES
from .package_index import PackageIndex
from .siblings import detect_siblings_by_rules
from .symbol_index import SymbolIndex


def _default_parsers() -> list[LanguageParser]:
    """Создать набор парсеров по умолчанию (java + python + go)."""
    return [JavaParser(), PythonParser(), GoParser()]


class GraphBuilder:
    """Строит NetworkX DiGraph из репозитория.

    Принимает опциональные парсеры и registry для DI (тесты, кастомные пайплайны).
    Без параметров использует все доступные парсеры и default_registry().

    Polyglot: каждый LanguageParser обрабатывает свой язык независимо.
    Java дополнительно проходит через PackageIndex для USES_DTO рёбер.
    """

    def __init__(self, parsers: list[LanguageParser] | None = None) -> None:
        self.graph: nx.DiGraph = nx.DiGraph()
        _parsers = parsers if parsers is not None else _default_parsers()
        self._parsers: dict[str, LanguageParser] = {
            p.supported_language: p for p in _parsers
        }
        self.stats: dict[str, int] = {
            "files_parsed": 0, "files_error": 0,
            "nodes": 0, "edges": 0,
        }

    def build(
        self,
        repo_root: Path | str,
        registry: ToolRegistry | None = None,
        langs: set[str] | None = None,
    ) -> nx.DiGraph:
        """Пройти репозиторий и построить граф.

        Args:
            repo_root: корень репозитория (train-ticket/)
            registry:  ToolRegistry с адаптерами; None → default_registry()
            langs:     фильтр языков; None = все языки из self._parsers
        """
        repo_root = Path(repo_root)
        # Используем языки всех зарегистрированных парсеров, если не задано явно
        _langs = langs or set(self._parsers.keys())

        _registry = registry if registry is not None else default_registry()
        _opts = AnalysisOptions()
        ingester = FindingIngester(self.graph, repo_root)

        # ── Шаг 1: парсим все языки, Java — отдельный список для PackageIndex ─
        # Двухшаговый build нужен для PackageIndex: сначала собрать все пакеты
        # по всем сервисам, потом строить USES_DTO рёбра с полным индексом.
        parsed_java: list[ParsedJavaFile] = []
        # Файлы других языков (Python, Go): (parsed, parser)
        parsed_other: list[object] = []

        for path, kind in walk(repo_root, _langs, config_detector=detect_config_type):
            service = service_name_of(path, repo_root)

            if kind.startswith("config:"):
                adapters = _registry.for_file(path)
                for adapter in adapters:
                    try:
                        result = adapter.analyze(path, _opts)
                        ingester.ingest(result)
                        if result.errors:
                            self.stats["config_errors"] = (
                                self.stats.get("config_errors", 0) + len(result.errors)
                            )
                    except Exception:
                        self.stats["config_errors"] = self.stats.get("config_errors", 0) + 1
                continue

            parser = self._parsers.get(kind)
            if parser is None:
                continue

            parsed = parser.parse(path, service)
            if parsed.error:
                self.stats["files_error"] += 1
            else:
                self.stats["files_parsed"] += 1

            if kind == "java":
                parsed_java.append(parsed)  # type: ignore[arg-type]
            else:
                parsed_other.append(parsed)

        # ── Шаг 2: строим PackageIndex и SymbolIndex из распарсенных файлов ──
        pkg_index = PackageIndex().build_from_parsed(parsed_java)
        self.stats.update(pkg_index.stats)
        sym_index = SymbolIndex().build_from_parsed(parsed_java)
        self.stats.update(sym_index.stats)

        # ── Шаг 3: добавляем Java в граф (с USES_DTO + ASYNC_MESSAGE) ────────
        for parsed in parsed_java:
            self._add_java(parsed, pkg_index, sym_index)

        # ── Шаг 4: добавляем остальные языки (Python, Go) ────────────────────
        for parsed in parsed_other:
            self._add_common(parsed)  # type: ignore[arg-type]

        istat = ingester.stats
        self.stats["findings_ingested"] = istat["ingested"]
        self.stats["findings_low_conf"] = istat["skipped_low_confidence"]

        self.stats["sibling_edges"] = self._add_siblings()
        self.stats["shares_resource_edges"] = self._add_shares_resource()
        self._annotate_arch_patterns()

        self.stats["nodes"] = self.graph.number_of_nodes()
        self.stats["edges"] = self.graph.number_of_edges()
        return self.graph

    # ─── Добавление файлов в граф ─────────────────────────────────────────────

    def _add_common(self, f: object) -> None:
        """Добавить результат парсинга для любого языка кроме Java.

        Работает со структурой ParsedFile: service, calls_services,
        endpoints, listen_port. Не требует class-иерархии.
        """
        service = getattr(f, "service", "")
        if not service:
            return
        svc_id = make_service_id(service)
        self._ensure_service(svc_id, service)

        # listen_port → атрибут SERVICE-узла
        port = getattr(f, "listen_port", 0)
        if port:
            self.graph.nodes[svc_id]["listen_port"] = port

        # CALLS_SERVICE рёбра
        for callee in getattr(f, "calls_services", []):
            callee_id = make_service_id(callee)
            self._ensure_service(callee_id, callee)
            if not self.graph.has_edge(svc_id, callee_id):
                self.graph.add_edge(svc_id, callee_id, edge_type="CALLS_SERVICE")

        # HAS_ENDPOINT рёбра (от SERVICE-узла, без class-контекста)
        for ep in getattr(f, "endpoints", []):
            ep_path = getattr(ep, "path", "")
            ep_method = getattr(ep, "method", "*")
            ep_id = NodeId(f"{service}::endpoint::{ep_method}:{ep_path}")
            self.graph.add_node(
                ep_id,
                **node_attrs(
                    "ENDPOINT",
                    http_method=ep_method,
                    path=ep_path,
                    handler=getattr(ep, "handler", ""),
                    service=service,
                    language=getattr(f, "language", ""),
                ),
            )
            if not self.graph.has_edge(svc_id, ep_id):
                self.graph.add_edge(svc_id, ep_id, edge_type="HAS_ENDPOINT")

    def _add_java(
        self,
        f: ParsedJavaFile,
        pkg_index: PackageIndex | None = None,
        sym_index: SymbolIndex | None = None,
    ) -> None:
        svc_id = make_service_id(f.service)
        self._ensure_service(svc_id, f.service)

        for cls in f.classes:
            cls_id = make_class_id(f.service, cls.fqn)
            try:
                # Правильный node_type вместо всегда "CLASS" (ADR-002, ADR-020)
                nt = resolve_node_type(
                    kind=cls.kind,
                    annotations=cls.annotations,
                    extends_class=cls.extends_class,
                    is_abstract=cls.is_abstract,
                    implements_interfaces=cls.implements_interfaces,
                )
                role = arch_role_from_package(cls.fqn)
                self.graph.add_node(
                    cls_id,
                    **node_attrs(
                        nt,
                        kind=cls.kind,
                        fqn=cls.fqn,
                        service=f.service,
                        annotations=cls.annotations,
                        extends_class=cls.extends_class,
                        implements=cls.implements_interfaces,
                        arch_role=role,
                        file=f.path,
                        parse_status="OK" if not f.error else "PARTIAL_PARSED",
                        deprecated=f.deprecated,
                        has_bug_note=f.has_bug_note,
                    ),
                )
                self.graph.add_edge(svc_id, cls_id, edge_type="CONTAINS")

                for method in cls.methods:
                    m_id = make_method_id(f.service, cls.fqn, method.name)
                    self.graph.add_node(
                        m_id,
                        **node_attrs(
                            "METHOD",
                            name=method.name,
                            service=f.service,
                            annotations=method.annotations,
                            arch_role=role,
                            file=f.path,
                            flow_steps=method.flow_steps or None,
                            logger_tags=method.logger_tags or None,
                        ),
                    )
                    self.graph.add_edge(cls_id, m_id, edge_type="CONTAINS")
            except Exception:
                # MinimalClassNode: не теряем класс при сбое типизации (ADR-019)
                if not self.graph.has_node(cls_id):
                    self.graph.add_node(
                        cls_id,
                        **node_attrs("CLASS", fqn=cls.fqn, service=f.service,
                                     file=f.path, parse_status="MINIMAL"),
                    )
                    self.graph.add_edge(svc_id, cls_id, edge_type="CONTAINS")

        # IMPLEMENTS рёбра: CLASS/REPOSITORY → INTERFACE (ADR-002)
        for cls in f.classes:
            if not cls.implements_interfaces:
                continue
            cls_id = make_class_id(f.service, cls.fqn)
            for iface_name in cls.implements_interfaces:
                if iface_name in _REPOSITORY_SUPERCLASSES_SET:
                    continue  # Spring Data internals — не добавляем
                # Ищем INTERFACE-узел с этим именем в графе (в том же или других сервисах)
                iface_id = make_class_id(f.service, f"{f.package}.{iface_name}" if f.package else iface_name)
                if self.graph.has_node(iface_id):
                    if not self.graph.has_edge(cls_id, iface_id):
                        self.graph.add_edge(cls_id, iface_id, edge_type="IMPLEMENTS")

        # CALLS_SERVICE рёбра: от SERVICE-узла (не от метода — пока не знаем от какого именно)
        for callee in f.calls_services:
            callee_svc_id = make_service_id(callee)
            self._ensure_service(callee_svc_id, callee)
            # Ребро от сервиса-источника к сервису-цели
            if not self.graph.has_edge(svc_id, callee_svc_id):
                self.graph.add_edge(svc_id, callee_svc_id, edge_type="CALLS_SERVICE")

        # Endpoints
        for ep in f.endpoints:
            ep_id = NodeId(f"{f.service}::endpoint::{ep.method}:{ep.path}")
            self.graph.add_node(
                ep_id,
                **node_attrs(
                    "ENDPOINT",
                    http_method=ep.method,
                    path=ep.path,
                    class_fqn=getattr(ep, "class_fqn", ""),
                    method_name=getattr(ep, "method_name", ""),
                    service=f.service,
                ),
            )
            cls_id = make_class_id(f.service, getattr(ep, "class_fqn", ""))
            self.graph.add_edge(cls_id, ep_id, edge_type="HAS_ENDPOINT")

        # USES_DTO: импорты из других ts-* сервисов → ребро USES_DTO
        if pkg_index and f.imports:
            seen_dto: set[str] = set()
            for fqn in f.imports:
                kind, target_svc = pkg_index.classify_import(fqn)
                if (kind == "service"
                        and target_svc
                        and target_svc != f.service
                        and target_svc not in seen_dto):
                    seen_dto.add(target_svc)
                    tgt_id = make_service_id(target_svc)
                    self._ensure_service(tgt_id, target_svc)
                    if not self.graph.has_edge(svc_id, tgt_id):
                        self.graph.add_edge(
                            svc_id, tgt_id,
                            edge_type="USES_DTO",
                            confidence=0.9,
                            source_tool="package_index",
                        )

        # ASYNC_MESSAGE: RabbitMQ convertAndSend → mq:: узел (task 16)
        if sym_index:
            for expr in f.sends_queue_exprs:
                queue = sym_index.resolve(expr, f.service) or (
                    expr if not expr[0].isupper() else None  # строковый литерал
                )
                if queue:
                    mq_id = NodeId(f"mq::{queue}")
                    if not self.graph.has_node(mq_id):
                        self.graph.add_node(
                            mq_id, **node_attrs("INFRA", infra_type="mq", queue=queue)
                        )
                    if not self.graph.has_edge(svc_id, mq_id):
                        self.graph.add_edge(
                            svc_id, mq_id,
                            edge_type="ASYNC_MESSAGE",
                            direction="send",
                            source_tool="java_rabbit",
                        )

            for expr in f.receives_queue_exprs:
                queue = sym_index.resolve(expr, f.service) or (
                    expr if not expr[0].isupper() else None
                )
                if queue:
                    mq_id = NodeId(f"mq::{queue}")
                    if not self.graph.has_node(mq_id):
                        self.graph.add_node(
                            mq_id, **node_attrs("INFRA", infra_type="mq", queue=queue)
                        )
                    if not self.graph.has_edge(mq_id, svc_id):
                        self.graph.add_edge(
                            mq_id, svc_id,
                            edge_type="ASYNC_MESSAGE",
                            direction="receive",
                            source_tool="java_rabbit",
                        )

    def _ensure_service(self, node_id: NodeId, service: str) -> None:
        if not self.graph.has_node(node_id):
            nt = "LIBRARY" if is_library_service(service) else "SERVICE"
            self.graph.add_node(node_id, **node_attrs(nt, service=service))

    # ─── Постобработка графа ──────────────────────────────────────────────────

    def _add_siblings(self) -> int:
        """SIBLING_OF рёбра по правилам именования (task 7b)."""
        services = [
            n for n, d in self.graph.nodes(data=True)
            if d.get("node_type") == "SERVICE" and n.startswith("ts-")
        ]
        pairs = detect_siblings_by_rules(services)
        added = 0
        for a, b, weight, reason in pairs:
            a_id = make_service_id(a)
            b_id = make_service_id(b)
            if not self.graph.has_node(a_id) or not self.graph.has_node(b_id):
                continue
            # Двустороннее ребро — BFS может идти в обе стороны
            for src, tgt in ((a_id, b_id), (b_id, a_id)):
                if not self.graph.has_edge(src, tgt):
                    self.graph.add_edge(
                        src, tgt,
                        edge_type="SIBLING_OF",
                        weight=weight,
                        reason=reason,
                    )
                    added += 1
        return added

    def _annotate_arch_patterns(self) -> None:
        """Проставить arch_pattern каждому SERVICE-узлу (ADR-004).

        Собирает arch_role всех дочерних узлов сервиса и вызывает detect_arch_pattern().
        """
        from collections import defaultdict
        svc_roles: defaultdict[str, dict[str, int]] = defaultdict(dict)
        for n, d in self.graph.nodes(data=True):
            svc = d.get("service", "")
            role = d.get("arch_role", "")
            if svc and role and role != "unknown":
                svc_roles[svc][role] = svc_roles[svc].get(role, 0) + 1
        for n, d in self.graph.nodes(data=True):
            if d.get("node_type") in ("SERVICE", "LIBRARY"):
                svc = d.get("service", "")
                roles = svc_roles.get(svc, {})
                d["arch_pattern"] = detect_arch_pattern(roles)

    def _add_shares_resource(self) -> int:
        """SHARES_RESOURCE рёбра для сервисов, разделяющих одну БД (task 7)."""
        from collections import defaultdict
        db_users: defaultdict[str, list[str]] = defaultdict(list)

        for src, tgt, d in self.graph.edges(data=True):
            if (d.get("edge_type") == "CALLS_DB"
                    and self.graph.nodes[src].get("node_type") == "SERVICE"):
                db_users[tgt].append(src)

        added = 0
        for db_id, users in db_users.items():
            if len(users) < 2:
                continue
            for i, a in enumerate(users):
                for b in users[i + 1:]:
                    if not self.graph.has_edge(a, b):
                        self.graph.add_edge(
                            a, b,
                            edge_type="SHARES_RESOURCE",
                            resource=db_id,
                            weight=0.5,
                        )
                        added += 1
        return added
