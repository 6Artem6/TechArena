"""Generate project passport from aggregated service passports.

Reads cache/service_passports.pkl → aggregates business_summaries per service module →
Qwen 3B → cache/project_passport.json

Output format:
{
    "domain": "Online train ticket booking platform",
    "main_features": ["ticket booking", "seat reservation", ...],
    "user_journeys": ["search → book → pay → cancel", ...]
}

Usage:
    python scripts/gen_project_passport.py
    python scripts/gen_project_passport.py --max-summaries 40
"""
from __future__ import annotations

import argparse
import json
import pickle
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src._config import cfg_cache_dir


def _service_from_filename(file_key: str) -> str:
    """'ts-order-service/OrderController.java:abc123' → 'order'."""
    fname = file_key.split(":")[0]  # strip hash
    parts = fname.split("/")
    if len(parts) >= 2:
        module = parts[0]  # e.g. "ts-order-service"
        tokens = [p for p in module.split("-") if p not in ("ts", "service", "other")]
        return tokens[0] if tokens else module
    # fallback: derive from controller filename
    ctrl = parts[-1].replace("Controller.java", "").lower()
    return ctrl


def aggregate_summaries(passports: dict, max_summaries: int) -> list[str]:
    """Collect unique business summaries, grouped by service, skip home/trivial."""
    by_service: dict[str, list[str]] = defaultdict(list)

    for file_key, methods in passports.items():
        if not isinstance(methods, dict):
            continue
        service = _service_from_filename(file_key)
        for method_name, info in methods.items():
            if not isinstance(info, dict):
                continue
            if method_name in ("home", "index"):
                continue
            summary = info.get("business_summary", "")
            if isinstance(summary, str) and len(summary) > 20:
                by_service[service].append(summary)

    # Pick representative summaries per service (first 3, dedup globally)
    seen: set[str] = set()
    result: list[str] = []
    for service in sorted(by_service):
        for summary in by_service[service][:3]:
            # Normalise for dedup: first 80 chars lowercase
            key = re.sub(r"\s+", " ", summary[:80].lower())
            if key not in seen:
                seen.add(key)
                result.append(f"[{service}] {summary}")
            if len(result) >= max_summaries:
                break
        if len(result) >= max_summaries:
            break

    return result


def build_passport_via_qwen(summaries: list[str], enc) -> dict:
    summaries_text = "\n".join(f"- {s}" for s in summaries)
    prompt = (
        "Given these microservice method summaries from a Java microservice application:\n"
        f"{summaries_text}\n\n"
        "Describe the overall project. Return JSON only:\n"
        '{"domain": "one-sentence description of the system\'s business domain", '
        '"main_features": ["feature 1", "feature 2", "feature 3", "feature 4", "feature 5"], '
        '"user_journeys": ["journey 1: step -> step -> step", "journey 2: ...", "journey 3: ..."]}\n'
        "Focus on business functionality. Limit to 5 main_features and 3 user_journeys."
    )
    schema = {
        "type": "object",
        "properties": {
            "domain": {"type": "string"},
            "main_features": {"type": "array", "items": {"type": "string"}},
            "user_journeys": {"type": "array", "items": {"type": "string"}},
        },
    }
    return enc._call_ollama(prompt, schema=schema, num_predict=400, timeout=60)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate project passport via Qwen")
    parser.add_argument("--max-summaries", type=int, default=50,
                        help="Max method summaries fed to Qwen (default: 50)")
    parser.add_argument("--no-qwen", action="store_true",
                        help="Dry-run: print aggregated summaries, skip Qwen")
    args = parser.parse_args()

    cache_dir = cfg_cache_dir()
    passports_path = cache_dir / "service_passports.pkl"
    out_path = cache_dir / "project_passport.json"

    if not passports_path.exists():
        print(f"[ERROR] {passports_path} не найден — запустите gen_service_passports.py",
              file=sys.stderr)
        sys.exit(1)

    print(f"Читаем {passports_path}...", file=sys.stderr)
    passports = pickle.loads(passports_path.read_bytes())
    print(f"  {len(passports)} файлов в паспортах", file=sys.stderr)

    summaries = aggregate_summaries(passports, args.max_summaries)
    print(f"  {len(summaries)} уникальных summaries для Qwen", file=sys.stderr)

    if args.no_qwen:
        print("\nAggregated summaries:", file=sys.stderr)
        for s in summaries:
            print(f"  {s}", file=sys.stderr)
        return

    from src.localization.qwen_encoder import QwenEncoder
    enc = QwenEncoder()

    print("Qwen 3B → project passport...", file=sys.stderr)
    try:
        passport = build_passport_via_qwen(summaries, enc)
    except Exception as e:
        print(f"[ERROR] Qwen failed: {e}", file=sys.stderr)
        sys.exit(1)

    if not passport or not passport.get("domain"):
        print("[ERROR] Qwen вернул пустой или неполный ответ", file=sys.stderr)
        print(f"  Raw: {passport}", file=sys.stderr)
        sys.exit(1)

    out_path.write_text(json.dumps(passport, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nСохранён → {out_path}", file=sys.stderr)
    print(f"  domain: {passport.get('domain')}", file=sys.stderr)
    print(f"  main_features ({len(passport.get('main_features', []))}): "
          f"{passport.get('main_features', [])}", file=sys.stderr)
    print(f"  user_journeys ({len(passport.get('user_journeys', []))}): "
          f"{passport.get('user_journeys', [])}", file=sys.stderr)


if __name__ == "__main__":
    main()
