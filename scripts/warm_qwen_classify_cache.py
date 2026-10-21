#!/usr/bin/env python3
"""Прогрев Qwen cls_v3 кеша для всех шагов из scenarios/*.json.

Запускать один раз перед удалением step_rules из domain_config.yaml.
После прогрева step_typer.py использует Qwen как primary classifier,
regex (step_rules) становится fallback только для unseen шагов.

Использование:
    python scripts/warm_qwen_classify_cache.py
    python scripts/warm_qwen_classify_cache.py --dry-run   # только вывод статистики
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

SCENARIOS = [ROOT / f"tt-challenge/scenarios/S{i}.json" for i in range(8)]


def collect_actions() -> list[str]:
    actions: list[str] = []
    for path in SCENARIOS:
        if not path.exists():
            print(f"  [skip] {path.name} не найден", file=sys.stderr)
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        for step in data.get("steps", []):
            action = step.get("action", "")
            if action:
                actions.append(action)
    return actions


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Только показать шаги, не вызывать Qwen")
    args = parser.parse_args()

    actions = collect_actions()
    unique = sorted(set(actions))
    print(f"Шагов всего: {len(actions)}, уникальных: {len(unique)}", file=sys.stderr)

    if args.dry_run:
        for a in unique:
            print(a)
        return

    from src.localization.qwen_encoder import QwenEncoder, StepParams

    enc = QwenEncoder()
    if not enc.enabled:
        print("[ERROR] Qwen недоступен (ни ollama, ни transformers) — кеш не прогреется",
              file=sys.stderr)
        sys.exit(1)

    # Уже закешированные пропускаем автоматически внутри batch_classify_steps
    results = enc.batch_classify_steps(unique, n_workers=4)
    enc.flush()

    step_types: Counter[str] = Counter(r.step_type for r in results)
    other_count = step_types.get("other", 0) + step_types.get("unknown", 0)

    print("\nРаспределение step_type:", file=sys.stderr)
    for st, cnt in step_types.most_common():
        print(f"  {st:25s} {cnt:3d}", file=sys.stderr)

    print(f"\nИтого записей в кеше: {len(enc._cache)}", file=sys.stderr)
    print(f"Нераспознанных (other/unknown): {other_count}/{len(unique)}", file=sys.stderr)

    if other_count > len(unique) * 0.15:
        print("\n[WARN] >15% шагов не распознаны — step_rules в YAML ещё нужен как fallback",
              file=sys.stderr)
    else:
        print("\n[OK] <15% шагов не распознаны — можно убрать step_rules из YAML",
              file=sys.stderr)


if __name__ == "__main__":
    main()
