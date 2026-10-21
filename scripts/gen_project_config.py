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
# Шаг 3: optional_service_groups — кластеризация через теги паспортов
# ---------------------------------------------------------------------------

# Стоп-токены: слишком общие, не являются feature-специфичными
_TAG_STOP = frozenset({
    "management", "retrieval", "service", "system", "operations", "data",
    "admin", "information", "handling", "specific", "creation", "deletion",
    "update", "access", "train", "order", "booking", "route", "station",
    "travel", "payment", "user", "ticket", "price", "seat", "cancel",
    "rebook", "basic", "auth", "security", "common", "notify", "send",
    "contact", "request", "lookup", "search", "query", "based", "list",
    "name", "type", "details", "config", "processing", "other", "inside",
    "record", "retrieve", "insert", "create", "delete", "batch",
    "unique", "identifier", "existing", "available", "specific",
})

# Core первые-токены в именах сервисов → не передавать в Qwen как optional кандидаты
# Проверяется только ПЕРВЫЙ токен нормализованного имени (prefix/suffix сняты)
_CORE_NAME_TOKENS = frozenset({
    "order", "travel", "route", "seat", "user", "auth", "security", "basic",
    "admin", "payment", "cancel", "execute", "rebook", "notify", "station",
    "train", "gateway", "config", "common", "wait", "inside", "preserve",
    "contacts", "delivery", "notification", "verification",
})

# Сохраняется в project_config.json, используется в шаге 6 (optional_patterns.json)
# Формат: {group_name: [synonym_tokens_for_regex]}
# Для нового проекта — генерируется автоматически; fallback на пустой dict.
_FEATURE_TOKENS: dict[str, list[str]] = {}  # заполняется derive_optional_groups_from_tags


def _load_svc_tag_tokens(
    modules: list[str], prefix: str, suffix: str
) -> dict[str, set[str]]:
    """Токены тегов из паспортов + токены из имени сервиса."""
    from src._config import cfg_cache_dir
    import pickle

    svc_tokens: dict[str, set[str]] = {m: set() for m in modules}
    sp_path = cfg_cache_dir() / "service_profiles_v2.pkl"
    if sp_path.exists():
        try:
            data = pickle.load(open(sp_path, "rb"))
            profs = data.get("profiles", data) if isinstance(data, dict) else data
            for mod in modules:
                if mod in profs:
                    for tag in getattr(profs[mod], "tags", []):
                        svc_tokens[mod].update(
                            t for t in tag.split("-") if len(t) >= 4
                        )
        except Exception as e:
            print(f"[WARN] service_profiles_v2.pkl: {e}", file=sys.stderr)
    # Дополнить токенами из имени сервиса (надёжный fallback)
    for mod in modules:
        norm = mod.removeprefix(prefix).removesuffix(suffix)
        svc_tokens[mod].update(p for p in norm.split("-") if len(p) >= 4)
    return svc_tokens


def _cluster_by_token_overlap(
    svc_tokens: dict[str, set[str]], n_modules: int
) -> dict[str, list[str]]:
    """Детерминированная кластеризация: token → [services] при 2 ≤ count ≤ n//6."""
    tok_svcs: dict[str, list[str]] = {}
    for mod, tokens in svc_tokens.items():
        for tok in tokens:
            tok_svcs.setdefault(tok, []).append(mod)
    max_count = max(4, n_modules // 6)
    return {
        tok: sorted(set(svcs))
        for tok, svcs in tok_svcs.items()
        if 2 <= len(svcs) <= max_count and tok not in _TAG_STOP
    }


def derive_optional_groups_from_tags(
    modules: list[str],
    prefix: str,
    suffix: str,
    no_qwen: bool = False,
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Кластеризовать сервисы по optional feature groups из тегов паспортов.

    Returns:
        groups         — {group_name: [service_names]}
        tokens_per_grp — {group_name: [synonym_tokens]} для шага 6 (optional_patterns.json)
    """
    svc_tokens = _load_svc_tag_tokens(modules, prefix, suffix)
    cluster_cands = _cluster_by_token_overlap(svc_tokens, len(modules))

    if no_qwen:
        # Без Qwen: вернуть кандидаты как есть (возможны ложные группы)
        tok_map = {tok: [tok] for tok in cluster_cands}
        return dict(cluster_cands), tok_map

    from src.localization.qwen_encoder import QwenEncoder
    enc = QwenEncoder()
    if not enc.enabled:
        print("[WARN] Qwen недоступен — optional_service_groups: детерминированный fallback",
              file=sys.stderr)
        return dict(cluster_cands), {tok: [tok] for tok in cluster_cands}

    module_set = set(modules)
    arr_schema: dict = {"type": "array", "items": {"type": "string"}}
    obj_schema: dict = {
        "type": "object",
        "additionalProperties": {"type": "string"},
    }

    # Qwen шаг 1: из кластеров выбрать optional addon feature groups
    cand_keys = sorted(cluster_cands.keys())
    cand_desc = "\n".join(
        f"  {tok}: {cluster_cands[tok]}" for tok in cand_keys
    )
    prompt1 = (
        f"Token clusters from microservice names/tags:\n{cand_desc}\n\n"
        f"Which token keys represent OPTIONAL BOOKING ADDON features "
        f"(things a passenger may ADD to a booking: food, insurance/assurance, luggage)?\n"
        f"Skip infrastructure groups (auth, payment, order, route, config, preserve).\n"
        f"Return ONLY a JSON array of token keys from the list above."
    )
    optional_tokens: list[str] = []
    try:
        raw1 = enc._call_ollama(prompt1, schema=arr_schema, num_predict=100, timeout=30)
        if isinstance(raw1, list):
            optional_tokens = [t for t in raw1 if t in set(cand_keys)]
            print(f"  Qwen выбрал кластеры: {optional_tokens}", file=sys.stderr)
    except Exception as e:
        print(f"[WARN] Qwen ошибка step1: {e}", file=sys.stderr)

    groups: dict[str, list[str]] = {tok: cluster_cands[tok] for tok in optional_tokens}
    clustered = {s for svcs in groups.values() for s in svcs}

    # Qwen шаг 2: singletons батчами по 12 — classify unclustered services
    # Отсеять core-сервисы по name-токенам перед передачей в Qwen
    def _is_core(mod: str) -> bool:
        norm = mod.removeprefix(prefix).removesuffix(suffix)
        first = norm.split("-")[0]
        # "travel2".startswith("travel") → core; "consign-price" → first="consign" → not core
        return any(first == tok or first.startswith(tok) for tok in _CORE_NAME_TOKENS)

    singletons = [m for m in sorted(modules) if m not in clustered and not _is_core(m)]
    batch_size = 12
    for i in range(0, len(singletons), batch_size):
        batch = singletons[i: i + batch_size]
        svc_desc = "\n".join(
            f"  {m}: {sorted(svc_tokens.get(m, []))[:4]}"
            for m in batch
        )
        prompt2 = (
            f"Classify these microservices — are they optional booking addons?\n{svc_desc}\n\n"
            f"Optional addons: things passengers add voluntarily (food ordering, "
            f"insurance/assurance, luggage consignment). NOT core booking infrastructure.\n"
            f"For each optional service assign a short group name (e.g. 'food', 'insurance', 'consign').\n"
            f"Skip core services entirely.\n"
            f"Return ONLY JSON: {{\"service-name\": \"group-name\", ...}}"
        )
        try:
            raw2 = enc._call_ollama(prompt2, schema=obj_schema, num_predict=200, timeout=30)
            if isinstance(raw2, dict):
                for svc, grp in raw2.items():
                    if svc in module_set and grp and isinstance(grp, str):
                        groups.setdefault(grp, []).append(svc)
                        print(f"  singleton → {svc}: group={grp}", file=sys.stderr)
        except Exception as e:
            print(f"[WARN] Qwen ошибка step2 batch {i}: {e}", file=sys.stderr)

    # Нормализовать
    groups = {k: sorted(set(v)) for k, v in groups.items() if v}

    # Детерминированный expand: добавить незахваченные сервисы чьё имя содержит
    # ПЕРЕСЕЧЕНИЕ name-токенов существующих членов группы (строгий критерий).
    captured = {s for svcs in groups.values() for s in svcs}
    for grp in list(groups.keys()):
        # Общие токены: intersection всех name-токенов текущих членов + group_name
        member_tok_sets = []
        for svc in groups[grp]:
            norm = svc.removeprefix(prefix).removesuffix(suffix)
            member_tok_sets.append({p for p in norm.split("-") if len(p) >= 4})
        if not member_tok_sets:
            continue
        shared = member_tok_sets[0].copy()
        for s in member_tok_sets[1:]:
            shared &= s
        shared.add(grp)  # group_name всегда в expand-ключах
        shared -= _TAG_STOP
        if not shared:
            continue
        for m in modules:
            if m in captured:
                continue
            norm = m.removeprefix(prefix).removesuffix(suffix)
            m_toks = {p for p in norm.split("-") if len(p) >= 4}
            if m_toks & shared:
                groups[grp].append(m)
                captured.add(m)
                print(f"  expand → {m}: group={grp} (via {m_toks & shared})", file=sys.stderr)
    groups = {k: sorted(set(v)) for k, v in groups.items() if v}

    # tokens_per_grp: intersection name-токенов членов группы + group_name.
    # Intersection гарантирует специфичность: для food {food,delivery}∩{food}∩{station,food} = {food}.
    tokens_per_grp: dict[str, list[str]] = {}
    for grp, svcs in groups.items():
        member_toks = []
        for svc in svcs:
            norm = svc.removeprefix(prefix).removesuffix(suffix)
            member_toks.append({p for p in norm.split("-") if len(p) >= 4 and p not in _TAG_STOP})
        if member_toks:
            shared = member_toks[0].copy()
            for s in member_toks[1:]:
                shared &= s
        else:
            shared = set()
        shared.add(grp)  # group_name always included
        tokens_per_grp[grp] = sorted(shared)

    return groups, tokens_per_grp


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

    # Шаг 3: optional_service_groups — кластеризация через теги паспортов
    print("Кластеризую optional_service_groups из тегов...", file=sys.stderr)
    opt_groups, opt_tokens_per_grp = derive_optional_groups_from_tags(
        modules, naming["prefix"], naming["suffix"], no_qwen=args.no_qwen
    )
    if opt_groups:
        for key, svcs in sorted(opt_groups.items()):
            print(f"  {key}: {svcs}", file=sys.stderr)
    else:
        print("  Групп не найдено", file=sys.stderr)

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
    if opt_groups:
        config["optional_service_groups"] = opt_groups
        # optional_feature_tokens сохраняем для шага 6 (optional_patterns.json)
        config["optional_feature_tokens"] = opt_tokens_per_grp
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
    # Источник токенов: opt_tokens_per_grp (из тегов паспортов + имён сервисов).
    # step_typer.py ожидает ключи: has_food, no_food, has_insurance, no_insurance,
    #   has_luggage, no_luggage (старый API: consign → luggage).
    # Маппинг group_name → pattern_key определяется по overlap токенов.
    _KNOWN_PATTERN_KEYS = {
        "food": "food", "meal": "food", "dining": "food",
        "insurance": "insurance", "assurance": "insurance", "coverage": "insurance",
        "consign": "luggage", "consignment": "luggage", "luggage": "luggage", "baggage": "luggage",
    }
    patterns: dict[str, str] = {}
    for grp, tokens in opt_tokens_per_grp.items():
        # Определить pattern_key: первый токен группы совпадающий с известным маппингом
        pat_key = _KNOWN_PATTERN_KEYS.get(grp)
        if pat_key is None:
            for tok in tokens:
                if tok in _KNOWN_PATTERN_KEYS:
                    pat_key = _KNOWN_PATTERN_KEYS[tok]
                    break
        if pat_key is None:
            pat_key = grp  # fallback: group_name as-is
        # Добавить все синонимы pat_key из маппинга (luggage/baggage для consign)
        synonyms = {t for t, k in _KNOWN_PATTERN_KEYS.items() if k == pat_key and len(t) >= 4}
        all_toks = sorted(set(tokens) | synonyms)
        tok_pat = "|".join(t for t in all_toks if len(t) >= 4)
        if not tok_pat:
            continue
        patterns[f"has_{pat_key}"] = tok_pat
        patterns[f"no_{pat_key}"] = rf"(?:without|no)\s*(?:{tok_pat})"
    opt_patterns_path = cfg_cache_dir() / "optional_patterns.json"
    opt_patterns_path.write_text(json.dumps(patterns, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Сохранено: {opt_patterns_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
