#!/usr/bin/env python3
"""Локальная оценка submission.json.

    python scripts/eval.py submission.json                    # S0: IoU; S1-S7: format+статистика
    python scripts/eval.py submission.json --vs other.json    # сравнение двух сдач по всем сценариям
    python scripts/eval.py --run                              # запустить src/predict.py на S0-S7 → оценить
    python scripts/eval.py --run --vs sample/submission.json  # то же + сравнение с baseline/sample
"""
import sys, json, argparse, subprocess, collections
from pathlib import Path

ROOT      = Path(__file__).parent.parent
PLATFORM  = ROOT / "tt-challenge" / "platform"
GOLD_PATH = ROOT / "tt-challenge" / "gold" / "S0.json"
SKELETON  = ROOT / "tt-challenge" / "skeleton" / "trainticket.json.gz"
SCENARIOS = sorted((ROOT / "tt-challenge" / "scenarios").glob("S*.json"))
TARGETS   = ROOT / "tt-challenge" / "targets" / "trainticket"
PREDICT   = ROOT / "src" / "predict.py"
SAMPLE    = ROOT / "sample" / "submission.json"

sys.path.insert(0, str(PLATFORM))
from evaluate_golden import load, evaluate, RELATIONS  # noqa: E402

COLS = {"executes": "exec", "calls": "call", "remote": "rem", "data": "data"}
W = 6


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _edge_stats(edges: list) -> dict:
    c = collections.Counter(e["relation"] for e in edges if isinstance(e, dict))
    return {r: c.get(r, 0) for r in RELATIONS}


def _iou_cells(sets: dict) -> str:
    return "  ".join(f"{sets[r]['iou']:>{W}.3f}" for r in RELATIONS)


def _stat_cells(stats: dict) -> str:
    return "  ".join(f"{stats.get(r, 0):>{W}}" for r in RELATIONS)


def _header_iou() -> str:
    cols = "  ".join(f"{COLS[r]:>{W}}" for r in RELATIONS)
    return f"{'сценарий':<10}  {cols}  {'avg':>{W}}  примечание"


def _header_stat() -> str:
    cols = "  ".join(f"{COLS[r]:>{W}}" for r in RELATIONS)
    return f"{'сценарий':<10}  {cols}  {'total':>{W}}"


# ---------------------------------------------------------------------------
# Основная функция оценки одного файла
# ---------------------------------------------------------------------------

def report(sub_path: Path, label: str, gold: dict, quiet: bool = False) -> dict:
    """Вернуть {sid: {score|None, stats}}. Напечатать таблицу."""
    sub = load(str(sub_path))
    R = evaluate(gold, sub)

    if R["status"] == "invalid":
        print(f"\n[{label}] ОШИБКА ФОРМАТА:")
        for e in R["errors"]:
            print(f"  - {e}")
        return {}

    if not quiet:
        print(f"\n{'='*72}")
        print(f"  {label}")
        print(f"{'='*72}")

    results = {}
    has_iou = bool(R["scored"])

    if has_iou and not quiet:
        print(_header_iou())
        print("-" * 72)

    for sid in sorted(sub.get("scenarios", {}).keys()):
        S = R["scenarios"].get(sid, {})
        edges = sub["scenarios"][sid].get("edges", [])
        stats = _edge_stats(edges)
        total = sum(stats.values())

        if S.get("status") == "scored":
            score = S["score"]
            note = ""
            for r in RELATIONS:
                v = S["sets"][r]
                if v["gold"] == 0 and v["pred"] > 0:
                    note += f" ⚠{COLS[r]}:pred={v['pred']},gold=0"
            if not quiet:
                print(f"  {sid:<8}  {_iou_cells(S['sets'])}  {score:>{W}.3f}  {note}")
                detail = "  ".join(f"{COLS[r]}:{S['sets'][r]['tp']}/{S['sets'][r]['gold']}" for r in RELATIONS)
                print(f"  {'':8}  tp/gold: {detail}")
            results[sid] = {"score": score, "stats": stats}
        else:
            if not quiet:
                print(f"  {sid:<8}  (нет gold)  exec={stats['executes']:>4} call={stats['calls']:>4} rem={stats['remote']:>3} data={stats['data']:>3}  total={total}")
            results[sid] = {"score": None, "stats": stats}

    if has_iou and not quiet:
        print("-" * 72)
        score = R["score"] or 0.0
        print(f"  {'S0 IoU':<8}  {'':>{W*4+6}}  {score:>{W}.3f}")

        for w in R.get("warnings", []):
            print(f"  ⚠  {w}")
        for sid, S in R["scenarios"].items():
            for w in S.get("warnings", []):
                print(f"  [{sid}] ⚠  {w}")

    return results


# ---------------------------------------------------------------------------
# Сравнение двух файлов
# ---------------------------------------------------------------------------

def compare(res_a: dict, label_a: str, res_b: dict, label_b: str) -> None:
    print(f"\n{'='*72}")
    print(f"  Сравнение: {label_a}  vs  {label_b}")
    print(f"{'='*72}")

    la = label_a[:16]
    lb = label_b[:16]
    print(f"  {'сцен':<6}  {'метрика':<10}  {la:>8}  {lb:>8}  {'Δ':>8}")
    print("-" * 60)

    for sid in sorted(set(res_a) | set(res_b)):
        a = res_a.get(sid, {})
        b = res_b.get(sid, {})

        if a.get("score") is not None and b.get("score") is not None:
            delta = a["score"] - b["score"]
            sign = "+" if delta >= 0 else ""
            print(f"  {sid:<6}  {'IoU avg':<10}  {a['score']:>8.3f}  {b['score']:>8.3f}  {sign}{delta:>7.3f}")

        # статистика edges
        for r in RELATIONS:
            va = a.get("stats", {}).get(r, 0)
            vb = b.get("stats", {}).get(r, 0)
            if va != vb:
                d = va - vb
                sign = "+" if d >= 0 else ""
                print(f"  {sid:<6}  {COLS[r]:<10}  {va:>8}  {vb:>8}  {sign}{d:>7}")

    print()


# ---------------------------------------------------------------------------
# Запуск предиктора
# ---------------------------------------------------------------------------

def run_predictor(out_path: Path) -> None:
    scenario_files = [str(p) for p in SCENARIOS]
    cmd = [sys.executable, str(PREDICT)] + scenario_files + ["-o", str(out_path)]
    print(f"Запуск: python src/predict.py S0..S7 → {out_path.name}", file=sys.stderr)
    result = subprocess.run(cmd, capture_output=True, text=True)
    sys.stderr.write(result.stderr)
    if result.returncode != 0:
        print("ОШИБКА предиктора:", result.stdout[-500:])
        sys.exit(1)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("submission", nargs="?", help="submission.json для оценки")
    ap.add_argument("--vs", metavar="FILE", help="второй файл для сравнения")
    ap.add_argument("--run", action="store_true", help="сначала запустить src/predict.py на S0-S7")
    a = ap.parse_args()

    gold = load(str(GOLD_PATH))

    sub_path = Path(a.submission) if a.submission else ROOT / "scripts" / "_submission.json"

    if a.run:
        run_predictor(sub_path)

    if not sub_path.exists():
        ap.error(f"файл не найден: {sub_path}  (используй --run или укажи путь)")

    res_a = report(sub_path, sub_path.name, gold)

    if a.vs:
        vs_path = Path(a.vs)
        res_b = report(vs_path, vs_path.name, gold, quiet=True)
        compare(res_a, sub_path.name, res_b, vs_path.name)


if __name__ == "__main__":
    main()
