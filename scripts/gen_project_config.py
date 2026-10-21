#!/usr/bin/env python3
"""Генерация cache/project_config.json из скелета и Qwen.

Выводит три блока, которые затем убираются из domain_config.yaml:
  - service_naming      (prefix/suffix из commonprefix имён сервисов)
  - func_variant_suffixes (diff нормализованных пар имён сервисов)
  - optional_service_groups (Qwen: группировка сервисов по опциональным фичам)

_config.py при загрузке YAML merges поверх него содержимое project_config.json,
поэтому изменения в YAML не нужны — достаточно запустить этот скрипт.

Использование:
    python scripts/gen_project_config.py
    python scripts/gen_project_config.py --no-qwen   # только детерминированная часть
    python scripts/gen_project_config.py --skeleton path/to/skeleton.json.gz
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Шаг 1: service_naming — prefix/suffix через commonprefix + majority_suffix
# ---------------------------------------------------------------------------

def _majority_suffix(names: list[str], threshold: float = 0.7) -> str:
    """Longest suffix присутствующий у ≥ threshold доли строк."""
    min_count = len(names) * threshold
    max_len = max((len(s) for s in names), default=0)
    best = ""
    for length in range(1, max_len + 1):
        counts: dict[str, int] = {}
        for s in names:
            if len(s) >= length:
                suf = s[-length:]
                counts[suf] = counts.get(suf, 0) + 1
        dominant = max(counts, key=counts.__getitem__)
        if counts[dominant] >= min_count:
            best = dominant
        else:
            break
    return best


def derive_service_naming(modules: list[str]) -> dict[str, str]:
    prefix = os.path.commonprefix(modules)
    suffix = _majority_suffix(modules, threshold=0.7)
    return {"prefix": prefix, "suffix": suffix}


# ---------------------------------------------------------------------------
# Шаг 2: func_variant_suffixes — diff нормализованных пар
# ---------------------------------------------------------------------------

# Исключить: не функциональные варианты (routing-суффиксы, роли)
_SUFFIX_EXCLUDE = frozenset({"other", "admin", "info", "new"})


def derive_func_variant_suffixes(modules: list[str], prefix: str, suffix: str) -> list[str]:
    norm = {m: m.removeprefix(prefix).removesuffix(suffix) for m in modules}
    suffixes: set[str] = set()
    for a_m, a in norm.items():
        for b_m, b in norm.items():
            if a == b:
                continue
            if b.startswith(a + "-"):
                suf = b[len(a) + 1:]
                if "-" not in suf and suf not in _SUFFIX_EXCLUDE:
                    suffixes.add(suf)
    return sorted(suffixes)


# ---------------------------------------------------------------------------
# Шаг 3: optional_service_groups — Qwen семантическая группировка
# ---------------------------------------------------------------------------

_GROUPING_SYSTEM = (
    "You are a microservice architecture analyst.\n"
    "Given a list of microservices, identify ALL services that implement\n"
    "OPTIONAL booking features (features a user may or may not choose per booking).\n\n"
    "Feature groups to find:\n"
    "  food     — any food ordering service (train food, station food, delivery, food menu)\n"
    "             synonyms in names: food, meal, dining\n"
    "  insurance — any insurance/assurance service\n"
    "             synonyms: insurance, assurance, coverage\n"
    "  consign  — any luggage/consignment service\n"
    "             synonyms: consign, consignment, luggage, baggage\n\n"
    "Rules:\n"
    "  - Include ALL services whose name contains a food/insurance/consign synonym.\n"
    "  - 'assurance' is a synonym for 'insurance' — include ts-assurance-service under 'insurance'.\n"
    "  - station-food and train-food are both food services — include them.\n"
    "  - Use keys: food, insurance, consign.\n"
    "  - Skip admin, auth, routing, order, payment, user, travel, seat, basic, security services.\n\n"
    "Output ONLY valid JSON: {\"feature_key\": [\"service-name\", ...], ...}\n"
    "No explanation, no markdown."
)


# Токены для детерминированного расширения (синонимы фич)
_FEATURE_TOKENS: dict[str, list[str]] = {
    "food":      ["food", "meal", "dining"],
    "insurance": ["insurance", "assurance", "coverage"],
    "consign":   ["consign", "luggage", "baggage"],
}


def _expand_groups_by_tokens(
    groups: dict[str, list[str]],
    modules: list[str],
    prefix: str,
    suffix: str,
) -> dict[str, list[str]]:
    """Дополнить группы: добавить сервисы которые Qwen пропустил, но чьё имя содержит токен фичи."""
    result = {k: list(v) for k, v in groups.items()}
    for feature, tokens in _FEATURE_TOKENS.items():
        existing = set(result.get(feature, []))
        for m in modules:
            if m in existing:
                continue
            norm = m.removeprefix(prefix).removesuffix(suffix)
            parts = set(norm.split("-"))
            if parts & set(tokens):
                result.setdefault(feature, []).append(m)
    return {k: sorted(v) for k, v in result.items() if v}


def derive_optional_groups_qwen(modules: list[str]) -> dict[str, list[str]]:
    """Спросить Qwen: какие сервисы группируются по опциональным фичам."""
    from src.localization.qwen_encoder import QwenEncoder, _CLASSIFY_SCHEMA

    enc = QwenEncoder()
    if not enc.enabled:
        print("[WARN] Qwen недоступен — optional_service_groups не обновится", file=sys.stderr)
        return {}

    services_str = "\n".join(f"  - {m}" for m in sorted(modules))
    prompt = f"Microservices:\n{services_str}\n\nJSON:"

    schema = {
        "type": "object",
        "additionalProperties": {
            "type": "array",
            "items": {"type": "string"},
        },
    }
    system_prompt = _GROUPING_SYSTEM

    try:
        raw = enc._call_ollama(
            prompt,
            schema=schema,
            system=system_prompt,
            num_predict=300,
            timeout=30,
        )
        if not isinstance(raw, dict):
            print(f"[WARN] Qwen вернул неожиданный тип: {type(raw)}", file=sys.stderr)
            return {}
        # Фильтрация: оставить только реальные имена модулей
        module_set = set(modules)
        result: dict[str, list[str]] = {}
        for key, svcs in raw.items():
            if not isinstance(svcs, list):
                continue
            valid = [s for s in svcs if s in module_set]
            if valid:
                result[key] = valid
        return result
    except Exception as e:
        print(f"[WARN] Qwen ошибка: {e}", file=sys.stderr)
        return {}


# ---------------------------------------------------------------------------
# Загрузка скелета
# ---------------------------------------------------------------------------

def load_modules(skeleton_path: Path) -> list[str]:
    with gzip.open(skeleton_path) as f:
        sk = json.load(f)
    modules = sorted({
        n.get("module", "")
        for n in sk["nodes"].values()
        if n.get("module", "")
    })
    return modules


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skeleton", default=str(ROOT / "tt-challenge/skeleton/trainticket.json.gz"))
    parser.add_argument("--no-qwen", action="store_true", help="Пропустить Qwen-этап")
    parser.add_argument("--out", default=None, help="Путь к выходному JSON (по умолчанию cache/project_config.json)")
    args = parser.parse_args()

    skeleton_path = Path(args.skeleton)
    if not skeleton_path.exists():
        sys.exit(f"Скелет не найден: {skeleton_path}")

    print(f"Загружаю скелет: {skeleton_path.name}", file=sys.stderr)
    modules = load_modules(skeleton_path)
    print(f"Модулей: {len(modules)}", file=sys.stderr)

    # Шаг 1: service_naming
    naming = derive_service_naming(modules)
    print(f"service_naming: prefix='{naming['prefix']}' suffix='{naming['suffix']}'", file=sys.stderr)

    # Шаг 2: func_variant_suffixes
    suffixes = derive_func_variant_suffixes(modules, naming["prefix"], naming["suffix"])
    print(f"func_variant_suffixes: {suffixes}", file=sys.stderr)

    # Шаг 3: optional_service_groups
    if args.no_qwen:
        opt_groups: dict[str, list[str]] = {}
        print("Qwen-этап пропущен (--no-qwen)", file=sys.stderr)
    else:
        print("Запрашиваю Qwen для optional_service_groups...", file=sys.stderr)
        opt_groups = derive_optional_groups_qwen(modules)
        if opt_groups:
            for key, svcs in sorted(opt_groups.items()):
                print(f"  {key}: {svcs}", file=sys.stderr)
        else:
            print("  Qwen не вернул групп", file=sys.stderr)

    # Сборка результата
    config: dict = {
        "service_naming": naming,
        "func_variant_suffixes": suffixes,
    }
    # Детерминированное расширение: добавить сервисы пропущенные Qwen по token-matching
    opt_groups = _expand_groups_by_tokens(opt_groups, modules, naming["prefix"], naming["suffix"])
    if opt_groups:
        print("После расширения:", file=sys.stderr)
        for key, svcs in sorted(opt_groups.items()):
            print(f"  {key}: {svcs}", file=sys.stderr)
        config["optional_service_groups"] = opt_groups

    # Сохранение
    from src._config import cfg_cache_dir
    out_path = Path(args.out) if args.out else cfg_cache_dir() / "project_config.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nСохранено: {out_path}", file=sys.stderr)
    print(json.dumps(config, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
