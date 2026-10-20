#!/usr/bin/env python3
"""Diff двух prediction JSON-файлов.

Использование:
    python scripts/diff_predictions.py old.json new.json [--scenario S5]
"""
import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


def load_edges(path: str) -> dict[str, dict[str, set[tuple]]]:
    """Загрузить edges по сценариям и типам → {sid: {rel: set((src, tgt))}}"""
    data = json.loads(Path(path).read_text())
    scens = data.get("scenarios", data)
    result: dict[str, dict[str, set]] = {}
    for sid, sc in scens.items():
        by_rel: dict[str, set] = defaultdict(set)
        for e in sc.get("edges", []):
            src = e.get("source", "")
            tgt = e.get("target", "")
            rel = e.get("relation", "?")
            by_rel[rel].add((src, tgt))
        result[sid] = dict(by_rel)
    return result


def short(nid: str) -> str:
    """method:ts-foo-service:a.b.C#bar() → foo.C#bar()"""
    parts = nid.split(":", 2)
    if len(parts) < 3:
        return nid
    svc = parts[1].replace("ts-", "").replace("-service", "")
    fqn = parts[2]
    cls = fqn.split(".")[-1].split("#")[0] if "." in fqn else fqn.split("#")[0]
    method = fqn.split("#")[-1].split("(")[0] if "#" in fqn else ""
    if method:
        return f"{svc}.{cls}#{method}()"
    if fqn.startswith("res:") or fqn.startswith("table:"):
        return f"{svc}/{fqn}"
    return f"{svc}.{fqn[:40]}"


def svc_of(nid: str) -> str:
    parts = nid.split(":", 2)
    return parts[1].replace("ts-", "").replace("-service", "") if len(parts) >= 2 else nid


def diff_scenario(sid: str, old: dict[str, set], new: dict[str, set]) -> None:
    rels = sorted(set(list(old) + list(new)))
    changed = False
    for rel in rels:
        o = old.get(rel, set())
        n = new.get(rel, set())
        added   = n - o
        removed = o - n
        if not added and not removed:
            continue
        if not changed:
            print(f"\n{'='*60}")
            print(f"Сценарий {sid}")
            print(f"{'='*60}")
            changed = True
        print(f"\n  [{rel}]  +{len(added)} −{len(removed)}")
        # Group by target service
        for sign, edges in [('+', added), ('-', removed)]:
            by_svc: dict[str, list[str]] = defaultdict(list)
            for src, tgt in sorted(edges):
                by_svc[svc_of(tgt)].append(f"{short(src)} → {short(tgt)}")
            for svc in sorted(by_svc):
                print(f"    {sign} [{svc}]")
                for line in by_svc[svc][:6]:
                    print(f"        {line}")
                if len(by_svc[svc]) > 6:
                    print(f"        ... ещё {len(by_svc[svc])-6}")
    if not changed:
        print(f"  {sid}: нет изменений")


def summary(old_all: dict, new_all: dict) -> None:
    print("\n=== СВОДКА ===")
    sids = sorted(set(list(old_all) + list(new_all)))
    for sid in sids:
        o = old_all.get(sid, {})
        n = new_all.get(sid, {})
        rels = sorted(set(list(o) + list(n)))
        parts = []
        for rel in rels:
            a = len(n.get(rel, set()) - o.get(rel, set()))
            r = len(o.get(rel, set()) - n.get(rel, set()))
            if a or r:
                parts.append(f"{rel}:+{a}−{r}")
        if parts:
            print(f"  {sid}: {', '.join(parts)}")
        else:
            print(f"  {sid}: без изменений")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("old")
    p.add_argument("new")
    p.add_argument("--scenario", "-s", default=None)
    args = p.parse_args()

    old_all = load_edges(args.old)
    new_all = load_edges(args.new)

    sids = [args.scenario] if args.scenario else sorted(set(list(old_all) + list(new_all)))

    summary(old_all, new_all)

    for sid in sids:
        diff_scenario(
            sid,
            old_all.get(sid, {}),
            new_all.get(sid, {}),
        )


if __name__ == "__main__":
    main()
