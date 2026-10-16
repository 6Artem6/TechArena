"""Индекс скелета микросервисного приложения.

Загружает skeleton.json[.gz] и предоставляет структуры данных для:
  - обхода графа (BFS через closure())
  - сопоставления URL-шаблонов с обработчиками (handlers_for())
  - формирования выходного JSON (submission())

Полная замена baseline_closure.Index — без зависимости от baseline-скриптов.
"""
from __future__ import annotations

import collections
import gzip
import json
import re
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Загрузка JSON
# ---------------------------------------------------------------------------

def load_json(path: str | Path) -> dict:
    """Загрузить skeleton.json или skeleton.json.gz."""
    p = str(path)
    with (gzip.open(p, "rt", encoding="utf-8") if p.endswith(".gz") else open(p, encoding="utf-8")) as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# URL routing helpers
# ---------------------------------------------------------------------------

def path_segs(p: str) -> list[str]:
    """Разбить URL на сегменты, отбросив query-string и пустые части."""
    return [s for s in re.split(r"[/]+", p.split("?")[0]) if s]


def _match(req: list[str], tpl: list[str]) -> bool:
    """Сопоставить сегменты запроса с Spring-шаблоном.

    `{x}` — любой один сегмент; `**` — хвост произвольной длины.
    """
    if not tpl:
        return not req
    t = tpl[0]
    if t == "**":
        return True
    if not req:
        return False
    if t.startswith("{") or t == "*" or t == req[0]:
        return _match(req[1:], tpl[1:])
    return False


def _ann_paths(anns: list[dict] | None) -> list[tuple[str, list[str]]]:
    """Извлечь (verb, [path, ...]) из списка Java-аннотаций *Mapping."""
    out: list[tuple[str, list[str]]] = []
    for a in anns or []:
        if a["name"].endswith("Mapping"):
            vals = re.findall(r'"([^"]*)"', a.get("args") or "")
            out.append((a["name"].replace("Mapping", "").upper(), vals or [""]))
    return out


# ---------------------------------------------------------------------------
# Основной класс
# ---------------------------------------------------------------------------

class Index:
    """Граф скелета: handlers, call/override/wiring рёбра, deployment units.

    Атрибуты (совместимы с baseline_closure.Index для drop-in замены):
      N        — dict[node_id, node_attrs]
      module   — dict[node_id, module_name]   (только method-узлы)
      handlers — list[(node_id, verb|None, path_segs)]
      calls    — defaultdict[method_id → set[method_id]]
      over     — defaultdict[target → set[implementors]]
      wiring   — defaultdict[edge_type → defaultdict[source → set[target]]]
      units    — dict[module_name → set[deployment_unit]]
      size     — Counter[deployment_unit → кол-во модулей]
      stmts    — defaultdict[method_id → list[stmt_id]]
    """

    def __init__(self, sk: dict) -> None:
        self.sk = sk
        N: dict[str, Any] = sk["nodes"]
        self.N = N

        # --- Handlers: методы с @*Mapping аннотациями ---
        self.handlers: list[tuple[str, str | None, list[str]]] = []
        for nid, n in N.items():
            if n["kind"] != "method":
                continue
            mp = _ann_paths(n.get("annotations"))
            if not mp:
                continue
            cls_paths = [
                v
                for verb, vals in _ann_paths(N[n["owner"]].get("annotations"))
                for v in vals
            ] or [""]
            for verb, vals in mp:
                for v in vals:
                    for cp in cls_paths:
                        self.handlers.append(
                            (nid, verb if verb != "REQUEST" else None, path_segs(cp + "/" + v))
                        )

        # --- Call sites ---
        self.cs_method: dict[str, str] = {
            nid: n["method"]
            for nid, n in N.items()
            if n["kind"] == "call_site"
        }

        # --- Рёбра ---
        self.calls: dict[str, set[str]] = collections.defaultdict(set)
        self.over: dict[str, set[str]] = collections.defaultdict(set)
        self.wiring: dict[str, dict[str, set[str]]] = collections.defaultdict(
            lambda: collections.defaultdict(set)
        )
        for e in sk["edges"]:
            etype = e["type"]
            if etype == "calls":
                self.calls[self.cs_method[e["source"]]].add(e["target"])
            elif etype == "overrides":
                self.over[e["target"]].add(e["source"])
            elif etype in ("remote_call", "message_delivery", "runtime_dispatch", "data_access"):
                src = self.cs_method.get(e["source"], e["source"])
                self.wiring[etype][src].add(e["target"])

        # --- Модули и deployment units ---
        self.module: dict[str, str] = {
            nid: n["module"] for nid, n in N.items() if n["kind"] == "method"
        }
        self.units: dict[str, set[str]] = {
            n["name"]: set(n.get("deployment_units") or [n["name"]])
            for n in N.values()
            if n["kind"] == "module"
        }
        self.size: collections.Counter = collections.Counter(
            u for us in self.units.values() for u in us
        )

        # --- Операторы (stmt) ---
        self.stmts: dict[str, list[str]] = collections.defaultdict(list)
        for e in sk["edges"]:
            if e["type"] == "contains" and e["target"].startswith("stmt:"):
                self.stmts[e["source"]].append(e["target"])

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    def unit_of(self, m: str) -> str:
        """Самый узкий deployment unit для метода (сервис, не сборка)."""
        mod = self.module[m]
        return min(self.units.get(mod, {mod}), key=lambda u: (self.size[u], u))

    def cha(self, m: str) -> set[str]:
        """Call Hierarchy Analysis: все конкретные переопределения метода."""
        out: set[str] = set()
        st = [m]
        while st:
            x = st.pop()
            for o in self.over.get(x, ()):
                if o not in out:
                    out.add(o)
                    st.append(o)
        return out

    def handlers_for(
        self, method: str | None, path: str, host: str | None = None
    ) -> set[str]:
        """Обработчики, чей Spring-шаблон совпадает с URL-путём запроса."""
        segs = path_segs(path)
        out: set[str] = set()
        for nid, verb, tpl in self.handlers:
            if host and host not in self.N[nid]["module"]:
                continue
            if (verb is None or method is None or verb == method) and _match(segs, tpl):
                out.add(nid)
        return out

    def handler_docs(self) -> dict[str, list[str]]:
        """Текст обработчика для BM25: путь×3, имя×2, модуль."""
        from .nlp import toks as _toks
        docs: dict[str, list[str]] = {}
        for nid, verb, tpl in self.handlers:
            docs.setdefault(nid, []).extend(
                _toks(" ".join(tpl)) * 3
                + _toks(self.N[nid]["name"]) * 2
                + _toks(self.N[nid]["module"])
            )
        return docs

    # ------------------------------------------------------------------
    # BFS-замыкание (baseline-совместимый вариант — используется как fallback)
    # ------------------------------------------------------------------

    def closure(
        self, entries: set[str]
    ) -> tuple[set[str], set[tuple[str, str]], set[tuple[str, str]], set[tuple[str, str]]]:
        """Обход в контексте deployment unit'а.

        → (методы, calls-рёбра, remote-рёбра, data-рёбра)
        """
        seen: set[tuple[str, str]] = set()
        edges: set[tuple[str, str]] = set()
        remote: set[tuple[str, str]] = set()
        data: set[tuple[str, str]] = set()
        st = [(m, self.unit_of(m)) for m in entries]
        while st:
            m, u = st.pop()
            if (m, u) in seen:
                continue
            seen.add((m, u))
            for t in self.calls.get(m, ()):
                for x in (
                    {t}
                    | self.cha(t)
                    | self.wiring["runtime_dispatch"].get(m, set())
                ):
                    if (
                        not self.N[x].get("abstract")
                        and u in self.units.get(self.module.get(x), ())
                    ):
                        edges.add((m, x))
                        st.append((x, u))
            for t in (
                self.wiring["remote_call"].get(m, set())
                | self.wiring["message_delivery"].get(m, set())
            ):
                remote.add((m, t))
                st.append((t, self.unit_of(t)))
            for t in self.wiring["data_access"].get(m, set()):
                data.add((m, t))
        return {m for m, _ in seen}, edges, remote, data

    # ------------------------------------------------------------------
    # Форматирование submission
    # ------------------------------------------------------------------

    def submission(
        self,
        scenario: str,
        methods: set[str],
        edges: set[tuple[str, str]],
        remote: set[tuple[str, str]],
        data: set[tuple[str, str]],
        method: str = "closure",
        **extra,
    ) -> dict:
        """Сформировать блок одного сценария для submission JSON."""
        out: dict = {"method": method, **extra, "edges": []}
        out["edges"] += [
            {"source": f"scenario:{scenario}", "relation": "executes", "target": m}
            for m in sorted(methods)
        ]
        out["edges"] += [
            {"source": s, "relation": "calls", "target": t}
            for s, t in sorted(edges)
        ]
        out["edges"] += [
            {"source": s, "relation": "remote", "target": t}
            for s, t in sorted(remote)
        ]
        out["edges"] += [
            {"source": s, "relation": "data", "target": t}
            for s, t in sorted(data)
        ]
        out["edges"] += [
            {"source": f"scenario:{scenario}", "relation": "executes", "target": st}
            for m in sorted(methods)
            for st in self.stmts.get(m, [])
        ]
        return out
