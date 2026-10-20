#!/usr/bin/env python3
"""Генерация FSM transitions из скелета train-ticket.

Строит два индекса из топологии скелета (без хардкода):
  forward:  entry_nid → [{via, to_nid, to_module, to_handler}]
  reverse:  to_nid   → [entry_nid, ...]

Алгоритм:
  1. Transitive closure через calls + overrides (interface→impl) внутри сервиса
  2. remote_call рёбра из достижимых методов → downstream handlers

Сохраняет cache/fsm_transitions.json.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict, deque
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.core.skeleton_index import Index, load_json


def build_impl_of(ix: Index) -> dict[str, set[str]]:
    """interface_nid → set(impl_nid) через рёбра overrides."""
    impl_of: dict[str, set[str]] = defaultdict(set)
    for e in ix.sk.get("edges", []):
        if e.get("type") == "overrides":
            impl_of[e["target"]].add(e["source"])
    return impl_of


def intra_reachable(start_nid: str, ix: Index, impl_of: dict) -> set[str]:
    """Transitive closure через calls внутри модуля + interface→impl переходы."""
    mod = ix.module.get(start_nid, "")
    visited: set[str] = set()
    queue: deque[str] = deque([start_nid])
    while queue:
        nid = queue.popleft()
        if nid in visited:
            continue
        visited.add(nid)
        for d in ix.calls.get(nid, set()):
            if ix.module.get(d) == mod and d not in visited:
                queue.append(d)
        for impl_nid in impl_of.get(nid, set()):
            if ix.module.get(impl_nid) == mod and impl_nid not in visited:
                queue.append(impl_nid)
    return visited


def main() -> None:
    sk_path = ROOT / "tt-challenge" / "skeleton" / "trainticket.json.gz"
    if not sk_path.exists():
        sys.exit(f"Скелет не найден: {sk_path}")

    print("Загружаю скелет...", flush=True)
    ix = Index(load_json(str(sk_path)))

    impl_of = build_impl_of(ix)
    rc_wiring = ix.wiring.get("remote_call", {})

    forward: dict[str, list[dict]] = {}
    reverse: dict[str, list[str]] = defaultdict(list)

    for entry_nid, verb, tpl in ix.handlers:
        mod = ix.module.get(entry_nid, "")
        entry_name = ix.N.get(entry_nid, {}).get("name", "?")

        reachable = intra_reachable(entry_nid, ix, impl_of)

        transitions: list[dict] = []
        for nid in reachable:
            remote = rc_wiring.get(nid, set())
            if not remote:
                continue
            via_name = ix.N.get(nid, {}).get("name", "?")
            via = None if nid == entry_nid else via_name
            for d in remote:
                dst_mod = ix.module.get(d, "")
                dst_name = ix.N.get(d, {}).get("name", "?")
                t = {
                    "via": via,
                    "to_nid": d,
                    "to_module": dst_mod,
                    "to_handler": dst_name,
                    "condition": None,
                }
                transitions.append(t)
                reverse[d].append(entry_nid)

        if transitions:
            forward[entry_nid] = transitions

    # Сохранить
    out = {
        "meta": {
            "total_entries": len(forward),
            "total_reverse": len(reverse),
        },
        "forward": forward,
        "reverse": {k: list(set(v)) for k, v in reverse.items()},
    }

    cache_dir = ROOT / "cache"
    cache_dir.mkdir(exist_ok=True)
    out_path = cache_dir / "fsm_transitions.json"
    with out_path.open("w") as f:
        json.dump(out, f, indent=2)

    print(f"Сохранено: {out_path}")
    print(f"  forward entries: {len(forward)}")
    print(f"  reverse entries: {len(reverse)}")

    # Показать пример для preserve
    for nid, trs in forward.items():
        name = ix.N.get(nid, {}).get("name", "")
        mod = ix.module.get(nid, "")
        if name == "preserve" and "preserve-service" in mod and "other" not in mod:
            print(f"\nПример [{mod}::{name}] ({len(trs)} transitions):")
            for t in trs[:8]:
                via = t["via"] or "DIRECT"
                print(f"  via::{via:35s} → {t['to_module']}::{t['to_handler']}")
            break


if __name__ == "__main__":
    main()
