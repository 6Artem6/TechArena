#!/usr/bin/env python3
"""Единый скрипт генерации всех offline кешей.

Pipeline (зависимости по порядку шагов):

  skeleton.json.gz + targets/*.java
      │
  1.  gen_fsm_transitions.py   → cache/fsm_transitions.json
  2.  gen_fsm_conditions.py    → аннотирует fsm_transitions.json (AST + Qwen)
  3.  gen_sibling_metadata.py  → cache/sibling_metadata.json
  4.  gen_service_profiles.py  → cache/service_profiles_v2.pkl
                                  cache/service_passports.pkl
                                  cache/project_profile.json
  5.  gen_project_passport.py  → cache/project_passport.json
  6.  gen_category_clusters.py → cache/auto_categories.json
  7.  gen_project_config.py    → cache/project_config.json
                                  cache/optional_patterns.json

Дополнительные (не в критическом пути):
  gen_step_token_bridges.py   → cache/step_token_bridges.pkl  (P3, опционально)
  gen_classification_rules.py → cache/generated_classification_rules.json  (аудит)

Usage:
    python scripts/gen_all_caches.py              # пропустить уже существующие
    python scripts/gen_all_caches.py --force       # всё заново
    python scripts/gen_all_caches.py --from-step 4 # начать с шага 4
    python scripts/gen_all_caches.py --only 1 3 7  # только указанные шаги
    python scripts/gen_all_caches.py --list        # показать статус кешей, не запускать
    python scripts/gen_all_caches.py --missing     # только отсутствующие (как default, но явно)
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


@dataclass
class Step:
    num: int
    name: str
    script: str
    outputs: list[str]             # пути относительно ROOT
    desc: str
    args: list[str] = field(default_factory=list)  # доп. аргументы скрипту
    requires: list[str] = field(default_factory=list)  # required outputs


STEPS: list[Step] = [
    Step(
        num=1, name="fsm_transitions",
        script="scripts/gen_fsm_transitions.py",
        outputs=["cache/fsm_transitions.json"],
        desc="FSM transitions из топологии скелета (детерминированно)",
    ),
    Step(
        num=2, name="fsm_conditions",
        script="scripts/gen_fsm_conditions.py",
        outputs=[],   # аннотирует fsm_transitions.json inplace
        desc="Аннотация FSM conditions (AST-анализ Java + Qwen)",
        requires=["cache/fsm_transitions.json"],
    ),
    Step(
        num=3, name="sibling_metadata",
        script="scripts/gen_sibling_metadata.py",
        outputs=["cache/sibling_metadata.json"],
        desc="G/D sibling пары сервисов (Qwen classification)",
    ),
    Step(
        num=4, name="service_profiles",
        script="scripts/gen_service_profiles.py",
        outputs=[
            "cache/service_profiles_v2.pkl",
            "cache/service_passports.pkl",
            "cache/project_profile.json",
        ],
        desc="ServiceProfile + паспорта методов + теги сервисов (Qwen per controller)",
    ),
    Step(
        num=5, name="project_passport",
        script="scripts/gen_project_passport.py",
        outputs=["cache/project_passport.json"],
        desc="Паспорт проекта из агрегированных паспортов сервисов (Qwen 3B)",
        requires=["cache/service_passports.pkl"],
    ),
    Step(
        num=6, name="category_clusters",
        script="scripts/gen_category_clusters.py",
        outputs=["cache/auto_categories.json"],
        desc="Bottom-Up токен-кластеры (BGE-M3 + Qwen + CrossEncoder)",
        requires=["cache/project_passport.json"],
    ),
    Step(
        num=7, name="project_config",
        script="scripts/gen_project_config.py",
        outputs=[
            "cache/project_config.json",
            "cache/optional_patterns.json",
        ],
        desc="Конфиг проекта + optional patterns (derive из скелета + Qwen)",
        requires=["cache/auto_categories.json", "cache/project_passport.json"],
    ),
]


def _status(step: Step) -> str:
    """'ok' / 'missing' / 'partial' в зависимости от наличия outputs."""
    if not step.outputs:
        return "ok"  # шаг только аннотирует — считаем done если requires есть
    missing = [o for o in step.outputs if not (ROOT / o).exists()]
    if not missing:
        return "ok"
    if len(missing) == len(step.outputs):
        return "missing"
    return "partial"


def _check_requires(step: Step) -> list[str]:
    return [r for r in step.requires if not (ROOT / r).exists()]


def print_status(steps: list[Step]) -> None:
    print(f"\n{'#':>3}  {'name':<25}  {'status':<10}  {'outputs'}")
    print("-" * 80)
    for s in steps:
        st = _status(s)
        icon = "✅" if st == "ok" else ("⚠️ " if st == "partial" else "❌")
        outs = ", ".join(Path(o).name for o in s.outputs) if s.outputs else "(inplace)"
        print(f"{s.num:>3}  {s.name:<25}  {icon} {st:<8}  {outs}")
        if s.requires:
            missing_req = _check_requires(s)
            if missing_req:
                print(f"     {'':25}  ⚠ requires: {', '.join(Path(r).name for r in missing_req)}")
    print()


def run_step(step: Step, force: bool) -> bool:
    """Запустить шаг. Возвращает True если успешно."""
    st = _status(step)
    if st == "ok" and not force:
        print(f"  [{step.num}] {step.name}: уже есть — пропуск", flush=True)
        return True

    missing_req = _check_requires(step)
    if missing_req:
        print(
            f"  [{step.num}] {step.name}: ❌ requires отсутствуют: "
            f"{', '.join(Path(r).name for r in missing_req)} — пропуск",
            flush=True,
        )
        return False

    action = "принудительно" if force and st == "ok" else "генерирую"
    print(f"\n{'='*60}", flush=True)
    print(f"  [{step.num}] {step.name}: {action} — {step.desc}", flush=True)
    print(f"{'='*60}", flush=True)

    t0 = time.time()
    cmd = [sys.executable, str(ROOT / step.script)] + step.args
    result = subprocess.run(cmd, cwd=str(ROOT))
    elapsed = time.time() - t0

    if result.returncode != 0:
        print(
            f"\n  ❌ [{step.num}] {step.name}: завершился с кодом {result.returncode} "
            f"({elapsed:.1f}с)",
            flush=True,
        )
        return False

    print(f"\n  ✅ [{step.num}] {step.name}: готово ({elapsed:.1f}с)", flush=True)
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true",
                    help="Перегенерировать все шаги, даже если кеш есть")
    ap.add_argument("--missing", action="store_true",
                    help="Запустить только отсутствующие (поведение по умолчанию)")
    ap.add_argument("--from-step", type=int, metavar="N",
                    help="Начать с шага N (пропустить 1..N-1)")
    ap.add_argument("--only", type=int, nargs="+", metavar="N",
                    help="Запустить только указанные номера шагов")
    ap.add_argument("--list", action="store_true",
                    help="Показать статус кешей и выйти")
    ap.add_argument("--no-qwen", action="store_true",
                    help="Передать --no-qwen во все Qwen-зависимые шаги")
    a = ap.parse_args()

    if a.list:
        print_status(STEPS)
        return

    # Выбрать шаги для запуска
    steps = STEPS
    if a.only:
        steps = [s for s in STEPS if s.num in a.only]
    elif a.from_step:
        steps = [s for s in STEPS if s.num >= a.from_step]

    # Передать --no-qwen подходящим скриптам
    if a.no_qwen:
        qwen_scripts = {
            "scripts/gen_sibling_metadata.py",
            "scripts/gen_service_profiles.py",
            "scripts/gen_project_passport.py",
            "scripts/gen_category_clusters.py",
            "scripts/gen_project_config.py",
        }
        for s in steps:
            if s.script in qwen_scripts and "--no-qwen" not in s.args:
                s.args.append("--no-qwen")

    print_status(STEPS)

    t_start = time.time()
    failed: list[str] = []
    skipped: list[str] = []

    for step in steps:
        ok = run_step(step, force=a.force)
        if not ok:
            failed.append(step.name)
            # Если шаг упал — следующие шаги которые от него зависят пропускаем
            for later in steps:
                if any(r in (step.outputs or []) for r in later.requires):
                    skipped.append(later.name)

    total = time.time() - t_start
    print(f"\n{'='*60}")
    print(f"Итог: {len(steps) - len(failed) - len(skipped)}/{len(steps)} успешно"
          f"  ({total:.1f}с)")
    if failed:
        print(f"  ❌ Ошибки:  {', '.join(failed)}")
    if skipped:
        print(f"  ⏭ Пропуски: {', '.join(skipped)}")
    if not failed:
        print("  Все кеши готовы.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
