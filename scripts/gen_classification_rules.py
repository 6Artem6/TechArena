"""Генерация classification_rules для domain_config.yaml через Qwen.

Вход: скелет проекта (skeleton.json.gz)
Выход: список rules в формате classification_rules

Портативность: никакого trainticket-хардкода.
Данные полностью из скелета: enum values, service names, key methods.
"""
from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src._config import cfg_cache_dir
from src.localization.qwen_encoder import QwenEncoder

# ---------------------------------------------------------------------------

def _extract_enum_summary(sk: dict) -> list[dict]:
    """Извлечь enum types и их constants из скелета.

    Ключ — полный FQN чтобы избежать коллизий при одинаковых именах.
    """
    # fqn → {short_name, [constant_names]}
    enum_fqns: dict[str, dict] = {}
    for node in sk["nodes"].values():
        if node.get("kind") == "type" and node.get("type_kind") == "enum":
            fqn = node.get("fqn", "")
            short = fqn.split(".")[-1]
            enum_fqns[fqn] = {"short": short, "values": []}

    for node in sk["nodes"].values():
        if node.get("kind") != "field":
            continue
        owner = node.get("owner", "")   # e.g. "type:ts-common:edu.fudan.common.entity.SeatClass"
        node_type = node.get("type", "")
        name = node.get("name", "")
        if not name or not name[0].isupper():
            continue
        # Совпадение: owner содержит FQN enum и type поля тоже FQN enum
        for fqn in enum_fqns:
            if fqn in owner and node_type == fqn:
                if name not in enum_fqns[fqn]["values"]:
                    enum_fqns[fqn]["values"].append(name)

    # Дедупликация: если несколько FQN с одним short name — берём тот у которого больше values
    seen_short: dict[str, dict] = {}
    for fqn, info in enum_fqns.items():
        if not info["values"]:
            continue
        short = info["short"]
        if short not in seen_short or len(info["values"]) > len(seen_short[short]["values"]):
            seen_short[short] = info

    return [{"enum": v["short"], "values": v["values"]} for v in seen_short.values()]


def _extract_service_roles(sk: dict, prefix: str = "ts-", suffix: str = "-service") -> list[dict]:
    """Извлечь имена сервисов и их ключевые методы (controller/service layer)."""
    svc_methods: dict[str, list[str]] = {}
    for node in sk["nodes"].values():
        if node.get("kind") != "method":
            continue
        mod = node.get("module", "")
        if not mod.startswith(prefix):
            continue
        name = node.get("name", "")
        owner = node.get("owner", "")
        if not name or name[0].islower() and len(name) < 4:
            continue
        # Только controller/service методы
        if not any(kw in owner for kw in ("Controller", "ServiceImpl", "Service")):
            continue
        # Пропустить boilerplate
        if name in ("main", "configure", "passwordEncoder", "restTemplate",
                    "corsConfigurer", "home", "getServiceUrl"):
            continue
        svc_methods.setdefault(mod, [])
        if name not in svc_methods[mod]:
            svc_methods[mod].append(name)

    result = []
    for mod, methods in sorted(svc_methods.items()):
        result.append({
            "service": mod,
            "role": mod.removeprefix(prefix).removesuffix(suffix),
            "methods": methods[:6],
        })
    return result


def _build_prompt(enums: list[dict], services: list[dict]) -> str:
    enum_lines = "\n".join(
        f"  {e['enum']}: {', '.join(e['values'])}"
        for e in enums
        if e["values"]
    )
    svc_lines = "\n".join(
        f"  {s['service']}: {', '.join(s['methods'])}"
        for s in services
    )
    few_shot = """\
=== EXAMPLE (follow this schema exactly) ===
[
  {
    "rule_id": "seat_first",
    "target_field": "seat_class",
    "target_value": "FIRST",
    "priority": 20,
    "group": "seat_class",
    "positive_patterns": ["\\\\b(first.?class|business.?class|1st.?class)\\\\b"],
    "negative_patterns": []
  },
  {
    "rule_id": "seat_second",
    "target_field": "seat_class",
    "target_value": "SECOND",
    "priority": 20,
    "group": "seat_class",
    "positive_patterns": ["\\\\b(second.?class|economy|2nd.?class|standard)\\\\b"],
    "negative_patterns": []
  },
  {
    "rule_id": "is_refused",
    "target_field": "is_refused",
    "target_value": true,
    "priority": 10,
    "group": null,
    "positive_patterns": ["\\\\bthe\\\\s+system\\\\s+(refuses|rejects|denies)\\\\b"],
    "negative_patterns": []
  }
]
RULES:
- target_value is a plain string or boolean, NEVER an object like {"value": ...}
- positive_patterns must match NATURAL LANGUAGE (e.g. "first class"), not code names (e.g. "FIRSTCLASS")
- Each pattern must use \\b word boundaries where applicable
"""

    # NL hints для seat classes из SeatClass enum
    nl_hints = (
        "Natural language hints for SeatClass enum values:\n"
        "  BUSINESS → 'business class', 'business seat'\n"
        "  FIRSTCLASS → 'first class', 'first-class', '1st class'\n"
        "  SECONDCLASS → 'second class', 'economy class', '2nd class', 'standard'\n"
        "  HARDSEAT → 'hard seat'\n"
        "  SOFTSEAT → 'soft seat'\n"
        "  HARDBED → 'hard sleeper', 'hard bed'\n"
        "  SOFTBED → 'soft sleeper', 'soft bed'\n"
        "  HIGHSOFTBED → 'high soft bed', 'premium sleeper'\n"
    )

    return (
        "You are analyzing a microservice application to generate classification rules.\n\n"
        "=== ENUMS FROM CODEBASE ===\n"
        f"{enum_lines}\n\n"
        "=== SERVICES AND KEY METHODS ===\n"
        f"{svc_lines}\n\n"
        f"{few_shot}\n"
        f"{nl_hints}\n"
        "=== TASK ===\n"
        "Generate classification_rules for the remaining seat classes from SeatClass enum "
        "(HARDSEAT, SOFTSEAT, HARDBED, SOFTBED, HIGHSOFTBED) plus any other booking attributes "
        "you can infer from the codebase that are not in the example.\n\n"
        "Rules schema: rule_id, target_field, target_value (PLAIN scalar only!), priority, group, "
        "positive_patterns (natural language regex), negative_patterns.\n"
        "Return ONLY a valid JSON array. No explanation."
    )


def main() -> None:
    sk_path = ROOT / "tt-challenge/skeleton/trainticket.json.gz"
    if not sk_path.exists():
        print(f"[ERROR] skeleton not found: {sk_path}", file=sys.stderr)
        sys.exit(1)

    with gzip.open(sk_path) as f:
        sk = json.load(f)

    enums = _extract_enum_summary(sk)
    services = _extract_service_roles(sk)

    print("=== Enum summary ===", file=sys.stderr)
    for e in enums:
        print(f"  {e['enum']}: {e['values']}", file=sys.stderr)

    print("\n=== Services ===", file=sys.stderr)
    for s in services:
        print(f"  {s['service']}: {s['methods']}", file=sys.stderr)

    enc = QwenEncoder()
    if not enc.enabled:
        print("[ERROR] Qwen недоступен", file=sys.stderr)
        sys.exit(1)

    prompt = _build_prompt(enums, services)
    print("\n=== Prompt (first 500 chars) ===", file=sys.stderr)
    print(prompt[:500], file=sys.stderr)

    schema = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "rule_id": {"type": "string"},
                "target_field": {"type": "string"},
                "target_value": {},
                "priority": {"type": "integer"},
                "group": {},
                "positive_patterns": {"type": "array", "items": {"type": "string"}},
                "negative_patterns": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["rule_id", "target_field", "target_value", "positive_patterns"],
        },
    }

    try:
        rules = enc._call_ollama(prompt, schema=schema, num_predict=1500, timeout=120)
    except Exception as e:
        print(f"[ERROR] Qwen: {e}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(rules, list):
        print(f"[ERROR] Qwen вернул не список: {rules}", file=sys.stderr)
        sys.exit(1)

    # Нормализовать target_value: {"value": x, "type": ...} → x
    for rule in rules:
        tv = rule.get("target_value")
        if isinstance(tv, dict) and "value" in tv:
            rule["target_value"] = tv["value"]

    # Исправить alternation без группы: \bA|B\b → \b(A|B)\b
    import re as _re
    _alt_fix = _re.compile(r'^(\\b)(.+\|.+)(\\b)$')
    for rule in rules:
        fixed = []
        for pat in rule.get("positive_patterns", []):
            m = _alt_fix.match(pat)
            if m:
                pat = rf"\b({m.group(2)})\b"
            fixed.append(pat)
        rule["positive_patterns"] = fixed

    print(f"\n=== Generated {len(rules)} rules ===", file=sys.stderr)
    print(json.dumps(rules, indent=2, ensure_ascii=False))

    # Сохранить в cache для последующего сравнения с текущими правилами
    out = cfg_cache_dir() / "generated_classification_rules.json"
    out.write_text(json.dumps(rules, indent=2, ensure_ascii=False))
    print(f"\nСохранено: {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
