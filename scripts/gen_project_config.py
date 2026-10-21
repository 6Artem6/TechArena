#!/usr/bin/env python3
"""Генерация cache/project_config.json из скелета и Qwen.

Выводит блоки, которые затем убираются из domain_config.yaml:
  - service_naming        (prefix/suffix из commonprefix имён сервисов)
  - func_variant_suffixes (diff нормализованных пар имён сервисов)
  - optional_service_groups (Qwen: группировка сервисов по опциональным фичам)
  - tier2_arch_tokens     (Qwen: архитектурные роли из частых токенов имён классов)
  - tier4_remote_tokens   (Qwen: инфра-токены аутентификации для языка проекта)

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
import re
import sys
from collections import Counter
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
# Шаг 4: tier2_arch_tokens — Qwen валидирует архитектурные токены
# ---------------------------------------------------------------------------

def _tokenize_camel(name: str) -> list[str]:
    # Убрать анонимные классы Java: "Impl$1" → пропустить весь токен
    if "$" in name:
        return []
    parts = re.sub(r"([A-Z])", r"_\1", name).replace("-", "_").split("_")
    return [t.lower() for t in parts if len(t) > 2 and "$" not in t]


def _extract_class_tokens(sk: dict, top_n: int = 80) -> list[str]:
    counts: Counter[str] = Counter()
    for node in sk["nodes"].values():
        if node.get("kind") != "type":
            continue
        # fqn = "pkg.ClassName" — берём только имя класса (после последней точки)
        fqn = node.get("fqn", "")
        class_name = fqn.rsplit(".", 1)[-1] if "." in fqn else fqn
        for tok in _tokenize_camel(class_name):
            counts[tok] += 1
    return [tok for tok, _ in counts.most_common(top_n)]


def _extract_method_tokens(sk: dict, top_n: int = 60, namespace_filter: str = "") -> list[str]:
    counts: Counter[str] = Counter()
    for nid, node in sk["nodes"].items():
        if node.get("kind") != "method":
            continue
        if namespace_filter and namespace_filter not in nid.lower():
            continue
        name = node.get("name", "")
        if "<" in name:  # пропустить <init>, <clinit>
            continue
        for tok in _tokenize_camel(name):
            counts[tok] += 1
    return [tok for tok, _ in counts.most_common(top_n)]


def _extract_domain_tokens(modules: list[str], prefix: str, suffix: str) -> set[str]:
    tokens: set[str] = set()
    for m in modules:
        tokens.update(m.removeprefix(prefix).removesuffix(suffix).split("-"))
    return tokens


def _detect_project_language(sk: dict) -> str:
    counts: Counter[str] = Counter()
    for node in sk["nodes"].values():
        fp = node.get("file", "")
        if not fp:
            continue
        if fp.endswith(".java"):
            counts["java"] += 1
        elif fp.endswith(".go"):
            counts["go"] += 1
        elif fp.endswith(".py"):
            counts["python"] += 1
        elif fp.endswith((".ts", ".tsx")):
            counts["typescript"] += 1
    if not counts:
        kinds = Counter(n.get("kind", "") for n in sk["nodes"].values())
        if kinds.get("type", 0) > 50:
            return "java"
        return "unknown"
    return counts.most_common(1)[0][0]


# Известные архитектурные токены — детерминированный базис для tier2
_ARCH_TOKEN_WHITELIST = frozenset({
    "controller", "service", "impl", "repository", "handler", "manager",
    "factory", "client", "server", "dto", "dao", "mapper", "provider",
    "util", "utils", "listener", "adapter", "interceptor", "proxy",
    "validator", "builder", "registry", "config",
})


def derive_tier2_arch_tokens(sk: dict, domain_tokens: set[str], no_qwen: bool) -> list[str]:
    # Все токены из имён классов скелета
    all_class_tokens = set(_extract_class_tokens(sk, top_n=300))
    # Детерминированный базис: whitelist ∩ скелет, без доменных токенов
    base = sorted(_ARCH_TOKEN_WHITELIST & all_class_tokens - domain_tokens)

    if no_qwen:
        return base

    # Кандидаты для Qwen-расширения: частые токены вне whitelist и не доменные
    extra_cands = [
        t for t in _extract_class_tokens(sk, top_n=80)
        if t not in _ARCH_TOKEN_WHITELIST and t not in domain_tokens
    ][:20]

    if not extra_cands:
        return base

    from src.localization.qwen_encoder import QwenEncoder
    enc = QwenEncoder()
    if not enc.enabled:
        print("[WARN] Qwen недоступен — tier2_arch_tokens: только детерминированный базис", file=sys.stderr)
        return base

    schema: dict = {"type": "array", "items": {"type": "string"}}
    prompt = (
        f"Java Spring Boot class name tokens — classify as architectural role or not:\n"
        f"{', '.join(extra_cands)}\n\n"
        f"TASK: Select tokens that name an ARCHITECTURAL COMPONENT ROLE.\n"
        f"INCLUDE: handler, manager, factory, client, server, dao, mapper, provider,\n"
        f"  listener, adapter, interceptor, proxy, validator, builder, registry\n"
        f"EXCLUDE: business domain terms, data/status words (result, data, list, type,\n"
        f"  status, record), verbs (get, add, send), app bootstraps (application, init),\n"
        f"  messaging (rabbit, queues), any framework-specific words\n\n"
        f"Return ONLY a JSON array of architectural role tokens from the input list."
    )
    try:
        raw = enc._call_ollama(prompt, schema=schema, num_predict=150, timeout=30)
        if isinstance(raw, list):
            qwen_extra = [t for t in raw if t in set(extra_cands)]
            return sorted(set(base) | set(qwen_extra))
    except Exception as e:
        print(f"[WARN] Qwen ошибка tier2: {e}", file=sys.stderr)
    return base


# ---------------------------------------------------------------------------
# Шаг 5: tier4_remote_tokens — Qwen генерирует инфра-токены для языка
# ---------------------------------------------------------------------------

# Известные auth/security инфра-токены — детерминированный базис для tier4
_AUTH_TOKEN_WHITELIST = frozenset({
    "auth", "authentication", "authorize", "authorization",
    "verify", "verification", "jwt", "token", "credential",
    "password", "secret", "captcha", "encoder", "username",
})


def derive_tier4_remote_tokens(sk: dict, language: str, no_qwen: bool) -> list[str]:
    # Все токены из имён классов и методов скелета
    all_class = set(_extract_class_tokens(sk, top_n=500))
    all_method = set(_extract_method_tokens(sk, top_n=500))
    all_tokens = all_class | all_method
    # Детерминированный базис: known auth whitelist ∩ реальные токены скелета
    base = sorted(_AUTH_TOKEN_WHITELIST & all_tokens)

    if no_qwen:
        return base or sorted(_AUTH_TOKEN_WHITELIST)

    # Кандидаты для Qwen-расширения: security/auth namespace токены вне whitelist
    sec = _extract_method_tokens(sk, top_n=60, namespace_filter="security")
    auth = _extract_method_tokens(sk, top_n=60, namespace_filter="auth")
    _exclude = {"cors", "configure", "mapping", "configurer", "get", "add",
                "find", "create", "delete", "all", "non", "default"}
    extra_cands = [
        t for t in sec + auth
        if t not in _AUTH_TOKEN_WHITELIST and t not in _exclude
    ][:15]

    if not extra_cands:
        return base

    from src.localization.qwen_encoder import QwenEncoder
    enc = QwenEncoder()
    if not enc.enabled:
        print("[WARN] Qwen недоступен — tier4_remote_tokens: только детерминированный базис", file=sys.stderr)
        return base

    schema: dict = {"type": "array", "items": {"type": "string"}}
    prompt = (
        f"Candidate tokens from {language} security/auth class and method names:\n"
        f"{', '.join(extra_cands)}\n\n"
        f"TASK: Which tokens relate to USER AUTHENTICATION or CREDENTIAL HANDLING?\n"
        f"INCLUDE: tokens like 'expir', 'refresh', 'claim', 'bearer', 'otp', 'role', 'principal'\n"
        f"EXCLUDE: general HTTP (cors, mapping), config terms (configure, configurer),\n"
        f"  common verbs (check, send, build), architectural roles (service, controller)\n\n"
        f"Return ONLY a JSON array of auth/credential tokens from the input list."
    )
    try:
        raw = enc._call_ollama(prompt, schema=schema, num_predict=150, timeout=30)
        if isinstance(raw, list):
            qwen_extra = [t for t in raw if t in set(extra_cands)]
            return sorted(set(base) | set(qwen_extra))
    except Exception as e:
        print(f"[WARN] Qwen ошибка tier4: {e}", file=sys.stderr)
    return base


# ---------------------------------------------------------------------------
# Загрузка скелета
# ---------------------------------------------------------------------------

def load_skeleton(skeleton_path: Path) -> tuple[dict, list[str]]:
    with gzip.open(skeleton_path) as f:
        sk = json.load(f)
    modules = sorted({
        n.get("module", "")
        for n in sk["nodes"].values()
        if n.get("module", "")
    })
    return sk, modules


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
    sk, modules = load_skeleton(skeleton_path)
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

    # Шаг 4: tier2_arch_tokens
    domain_tokens = _extract_domain_tokens(modules, naming["prefix"], naming["suffix"])
    language = _detect_project_language(sk)
    print(f"Язык проекта: {language}", file=sys.stderr)

    if args.no_qwen:
        arch_tokens: list[str] = derive_tier2_arch_tokens(sk, domain_tokens, no_qwen=True)
        print("tier2_arch_tokens: детерминированный fallback (--no-qwen)", file=sys.stderr)
    else:
        print("Запрашиваю Qwen для tier2_arch_tokens...", file=sys.stderr)
        arch_tokens = derive_tier2_arch_tokens(sk, domain_tokens, no_qwen=False)
    print(f"tier2_arch_tokens: {arch_tokens}", file=sys.stderr)

    # Шаг 5: tier4_remote_tokens
    if args.no_qwen:
        remote_tokens: list[str] = derive_tier4_remote_tokens(sk, language, no_qwen=True)
        print("tier4_remote_tokens: детерминированный fallback (--no-qwen)", file=sys.stderr)
    else:
        print("Запрашиваю Qwen для tier4_remote_tokens...", file=sys.stderr)
        remote_tokens = derive_tier4_remote_tokens(sk, language, no_qwen=False)
    print(f"tier4_remote_tokens: {remote_tokens}", file=sys.stderr)

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
    if arch_tokens:
        config["tier2_arch_tokens"] = arch_tokens
    if remote_tokens:
        config["tier4_remote_tokens"] = remote_tokens

    # Сохранение
    from src._config import cfg_cache_dir
    out_path = Path(args.out) if args.out else cfg_cache_dir() / "project_config.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nСохранено: {out_path}", file=sys.stderr)
    print(json.dumps(config, ensure_ascii=False, indent=2))

    # Шаг 6: optional_patterns.json — regex для has_*/no_* ключей (step_typer.py)
    # Детерминированно из _FEATURE_TOKENS; no_* = "without|no" + те же токены
    # consign → luggage: step_typer ожидает ключ "luggage"
    _FEATURE_TO_PATTERN_KEY: dict[str, str] = {
        "food": "food", "insurance": "insurance", "consign": "luggage",
    }
    _FEATURE_TOKENS_EXT: dict[str, list[str]] = {
        **_FEATURE_TOKENS,
        "consign": sorted(set(_FEATURE_TOKENS["consign"]) | {"luggage"}),
    }
    patterns: dict[str, str] = {}
    for feature, tokens in _FEATURE_TOKENS_EXT.items():
        key = _FEATURE_TO_PATTERN_KEY.get(feature, feature)
        tok_pat = "|".join(tokens)
        patterns[f"has_{key}"] = tok_pat
        patterns[f"no_{key}"] = rf"(?:without|no)\s*(?:{tok_pat})"
    opt_patterns_path = cfg_cache_dir() / "optional_patterns.json"
    opt_patterns_path.write_text(json.dumps(patterns, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Сохранено: {opt_patterns_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
