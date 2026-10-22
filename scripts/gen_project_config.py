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
from collections import Counter, defaultdict
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

# NLP-базис стоп-токенов: слова слишком общие для любого проекта.
# Доменные стоп-токены (train, order, booking и т.д.) вычисляются
# динамически через derive_high_freq_tokens() — они специфичны для проекта.
def _load_tag_stop_base() -> frozenset[str]:
    """NLP baseline стоп-слова для тегов сервисов — загружаются из domain_config.yaml."""
    from src._config import cfg_get as _cfg_get
    base = frozenset(_cfg_get("tag_stop_base", []))
    infra = frozenset(_cfg_get("infra_module_tokens", []))
    return base | infra


def _load_infra_module_tokens() -> frozenset[str]:
    """First-tokens инфра-модулей — из project_config.json если есть, иначе yaml fallback."""
    from src._config import cfg_cache_dir as _ccd, cfg_get as _cfg_get
    import json as _j
    pc_path = _ccd() / "project_config.json"
    if pc_path.exists():
        try:
            pc = _j.loads(pc_path.read_text(encoding="utf-8"))
            if "infra_module_tokens" in pc:
                return frozenset(pc["infra_module_tokens"])
        except Exception:
            pass
    return frozenset(_cfg_get("infra_module_tokens", []))


def derive_infra_module_tokens(
    modules: list[str],
    prefix: str,
    suffix: str,
    opt_groups: dict[str, list[str]],
    auto_categories_path: "Path",
    no_qwen: bool = False,
) -> list[str]:
    """Auto-derive infra module first_tokens: set subtraction + Qwen confirmation.

    1. all_first_toks из модулей скелета
    2. candidates = all_first - opt_group_keys - verified_cluster_toks
    3. Qwen: из candidates отобрать только cross-cutting инфра сервисы
    Portable: для нового репозитория не нужно угадывать инфра-список.
    """
    import json as _j

    def _first(mod: str) -> str:
        norm = mod.removeprefix(prefix).removesuffix(suffix).removesuffix("-other")
        return norm.split("-")[0] if norm else ""

    all_first = {_first(m) for m in modules if len(_first(m)) >= 3}

    # Убрать optional group keys (food, consign, insurance — явный бизнес-домен)
    opt_keys = set(opt_groups.keys())

    # Убрать verified cluster tokens (бизнес-домен, auto-discovered)
    cluster_toks: set[str] = set()
    if auto_categories_path.exists():
        try:
            auto = _j.loads(auto_categories_path.read_text(encoding="utf-8"))
            cluster_toks = {
                t for cat in auto if cat.get("verified")
                for t in cat.get("tokens", [])
            }
        except Exception:
            pass

    candidates = sorted(all_first - opt_keys - cluster_toks)
    print(f"  infra candidates ({len(candidates)}): {candidates}", file=sys.stderr)

    if no_qwen or not candidates:
        return candidates

    from src.localization.qwen_encoder import QwenEncoder
    enc = QwenEncoder()
    if not enc.enabled:
        return candidates

    prompt = (
        f"From these microservice name tokens: {candidates}\n"
        "Select ALL cross-cutting infrastructure tokens — services that handle technical "
        "concerns shared across the entire application, not domain business logic.\n"
        "Infrastructure examples: auth (authentication), gateway (API gateway), "
        "notification (alerts/emails), execute (job execution), wait (queue/waitlist), "
        "verification (code/captcha verification), delivery (message delivery), common (shared utils).\n"
        "Domain business examples to EXCLUDE: order, cancel, seat, user, route, preserve, "
        "rebook, inside, config, contacts, travel, price, admin.\n"
        "Return JSON array of selected infrastructure tokens only."
    )
    schema = {"type": "array", "items": {"type": "string"}}
    try:
        result = enc._call_ollama(prompt, schema=schema, num_predict=100, timeout=30)
        if isinstance(result, list):
            valid = [t for t in result if t in set(candidates)]
            if valid:
                return valid
    except Exception as e:
        print(f"[WARN] derive_infra_module_tokens Qwen: {e}", file=sys.stderr)

    return candidates


def derive_high_freq_tokens(
    svc_tokens: dict[str, set[str]], n_modules: int, threshold: float = 0.40
) -> frozenset[str]:
    """Токены встречающиеся у >= threshold доли сервисов → слишком generic → стоп."""
    tok_count: Counter[str] = Counter()
    for tokens in svc_tokens.values():
        for tok in tokens:
            tok_count[tok] += 1
    min_count = max(2, int(n_modules * threshold))
    return frozenset(tok for tok, cnt in tok_count.items() if cnt >= min_count)


def derive_core_name_tokens(
    modules: list[str], prefix: str, suffix: str
) -> frozenset[str]:
    """Первые токены нормализованных имён сервисов, встречающиеся у ≥3 модулей.

    Эти токены являются core-сервисами проекта (admin, …).
    Порог ≥3 намеренно исключает optional addon пары (food×2, consign×2)
    которые имеют только 2 варианта (base + functional-extension).
    Core G/OTHER пары (order-service / order-other-service) тоже count=2
    → они не попадают в core_name_toks, но Qwen их отсеивает в optional-filter
    т.к. они не являются "optional booking addons".
    """
    first_tok_count: Counter[str] = Counter()
    for mod in modules:
        norm = mod.removeprefix(prefix).removesuffix(suffix)
        first = norm.split("-")[0]
        if len(first) >= 4:
            first_tok_count[first] += 1
    return frozenset(tok for tok, cnt in first_tok_count.items() if cnt >= 3)

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
    svc_tokens: dict[str, set[str]], n_modules: int, stop_tokens: frozenset[str]
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
        if 2 <= len(svcs) <= max_count and tok not in stop_tokens
    }


# Seed keywords для optional addon categories — используются для singleton pre-filter.
# Минимальный хардкод: только 3 известных addon типа. Actual keyword extraction —
# из имён методов/DTO в скелете через _has_optional_keyword_in_skeleton().
_OPTIONAL_SEED_KEYWORDS = frozenset({
    "food", "meal", "dining", "catering",
    "insurance", "assurance", "protection",
    "consign", "consignment", "luggage", "baggage",
})


def _has_optional_keyword_in_skeleton(sk: dict, module: str) -> bool:
    """True если сервис имеет методы/DTO/пути с optional addon keyword.

    Portable: ищет в именах методов и классов из скелета (не hardcoded сервисы).
    Фильтрует SecurityConfig/Application классы (только config/application suffix).
    """
    for node in sk["nodes"].values():
        if node.get("module") != module:
            continue
        kind = node.get("kind", "")
        if kind not in ("method", "type", "endpoint", "field"):
            continue
        # Пропустить bootstrap и config классы
        fqn = node.get("fqn", "").lower()
        if fqn.endswith(("application", "config", "configuration", "securityconfig")):
            continue
        for field in ("name", "fqn", "url"):
            val = node.get(field, "").lower()
            for kw in _OPTIONAL_SEED_KEYWORDS:
                if kw in val:
                    return True
    return False


def derive_optional_groups_from_tags(
    modules: list[str],
    prefix: str,
    suffix: str,
    no_qwen: bool = False,
    sk: dict | None = None,
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Кластеризовать сервисы по optional feature groups из тегов паспортов.

    Returns:
        groups         — {group_name: [service_names]}
        tokens_per_grp — {group_name: [synonym_tokens]} для шага 6 (optional_patterns.json)
    """
    svc_tokens = _load_svc_tag_tokens(modules, prefix, suffix)

    # Динамические стоп-токены: NLP-базис + высококорреляционные domain-токены + core инфра.
    # Не хардкодим доменные токены (train/order/station/…) — вычисляем из скелета.
    core_name_toks = derive_core_name_tokens(modules, prefix, suffix)
    high_freq = derive_high_freq_tokens(svc_tokens, len(modules), threshold=0.40)
    stop_tokens: frozenset[str] = _load_tag_stop_base() | high_freq | core_name_toks

    cluster_cands = _cluster_by_token_overlap(svc_tokens, len(modules), stop_tokens)

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

    # Детерминированный pre-filter: сервисы с "-other" вариантом = core G/OTHER pairs
    norm_set_inner = {m.removeprefix(prefix).removesuffix(suffix) for m in modules}
    other_pair_first: set[str] = set()
    for m in modules:
        norm = m.removeprefix(prefix).removesuffix(suffix)
        if norm + "-other" in norm_set_inner:
            other_pair_first.add(norm.split("-")[0])

    # Qwen шаг 1: из кластеров выбрать optional addon feature groups
    cand_keys = sorted(cluster_cands.keys())
    cand_desc = "\n".join(
        f"  {tok}: {cluster_cands[tok]}" for tok in cand_keys
    )
    prompt1 = (
        f"Token clusters from microservice names/tags:\n{cand_desc}\n\n"
        f"Select token keys that represent OPTIONAL BOOKING ADDON features.\n"
        f"There are exactly 3 types of optional addons passengers may add to a ticket:\n"
        f"  1. FOOD/MEAL: food ordering, dining (keywords: food, meal, dining)\n"
        f"  2. INSURANCE/ASSURANCE: travel protection (keywords: insurance, assurance)\n"
        f"  3. LUGGAGE/CONSIGNMENT: baggage handling (keywords: consign, consignment, luggage)\n"
        f"Select ONLY clusters matching these 3 types. "
        f"SKIP: auth, payment, order, route, config, preserve, booking, station, travel.\n"
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

    # Qwen шаг 2: singletons — только сервисы НЕ в кластерах и НЕ core
    # Core: infra tokens + G/OTHER pairs + high-freq name tokens
    infra_toks = _load_infra_module_tokens()
    all_core_toks = core_name_toks | other_pair_first | infra_toks

    def _is_core(mod: str) -> bool:
        norm = mod.removeprefix(prefix).removesuffix(suffix)
        first = norm.split("-")[0]
        return any(first == tok or first.startswith(tok) for tok in all_core_toks)

    # Дополнительный фильтр: только сервисы у которых keyword match в методах/DTO/именах
    # с известными optional addon categories — portable search из скелета.
    if sk is not None:
        singletons = [
            m for m in sorted(modules)
            if m not in clustered
            and not _is_core(m)
            and _has_optional_keyword_in_skeleton(sk, m)
        ]
    else:
        singletons = [m for m in sorted(modules) if m not in clustered and not _is_core(m)]
    batch_size = 8
    for i in range(0, len(singletons), batch_size):
        batch = singletons[i: i + batch_size]
        svc_desc = "\n".join(
            f"  {m}: {sorted(svc_tokens.get(m, []))[:4]}"
            for m in batch
        )
        prompt2 = (
            f"Classify these microservices as optional booking addons (yes/no + group).\n"
            f"{svc_desc}\n\n"
            f"Optional addons are things passengers add VOLUNTARILY:\n"
            f"  food/meal → group 'food'\n"
            f"  insurance/assurance → group 'insurance'\n"
            f"  luggage/consignment → group 'consign'\n"
            f"SKIP core infrastructure (station, route, train, seat, user, cancel, rebook, delivery).\n"
            f"Return ONLY JSON for OPTIONAL services: {{\"service-name\": \"group-name\"}}"
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
        shared -= stop_tokens
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
            member_toks.append({p for p in norm.split("-") if len(p) >= 4 and p not in stop_tokens})
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


def _extract_arch_suffix_candidates(sk: dict, modules: list[str]) -> list[str]:
    """Суффиксы camelCase имён классов встречающиеся в >= 20% модулей → arch candidates.

    Portable: работает без хардкода доменных слов — суффиксы из скелета конкретного проекта.
    Суффиксы по N сервисам (не просто count) устойчивее к диспропорциям размера сервисов.
    """
    svc_has: defaultdict[str, set[str]] = defaultdict(set)
    for node in sk["nodes"].values():
        if node.get("kind") not in ("type", "class", "interface"):
            continue
        fqn = node.get("fqn", "")
        cls = fqn.rsplit(".", 1)[-1] if "." in fqn else fqn
        if "$" in cls:
            continue
        parts = re.sub(r"([a-z])([A-Z])", r"\1 \2", cls).split()
        if not parts:
            continue
        suf = parts[-1].lower()
        if len(suf) < 3:
            continue
        mod = node.get("module", "")
        if mod:
            svc_has[suf].add(mod)
    threshold = max(2, len(modules) * 0.10)
    return [
        suf for suf, svcs in sorted(svc_has.items(), key=lambda x: -len(x[1]))
        if len(svcs) >= threshold
    ]


def derive_tier2_arch_tokens(sk: dict, domain_tokens: set[str], no_qwen: bool) -> list[str]:
    from src._config import cfg_get as _cfg_get
    modules_list = sorted({n.get("module", "") for n in sk["nodes"].values() if n.get("module")})
    arch_stop = frozenset(_cfg_get("arch_suffix_stop", []))
    arch_extra = frozenset(_cfg_get("arch_token_extra", []))
    auto = {
        s for s in _extract_arch_suffix_candidates(sk, modules_list)
        if s not in domain_tokens and s not in arch_stop
    }
    return sorted(auto | (arch_extra - domain_tokens))


# ---------------------------------------------------------------------------
# Шаг 5: tier4_remote_tokens — Qwen генерирует инфра-токены для языка
# ---------------------------------------------------------------------------

def _derive_method_noise(sk: dict, all_modules: set[str], threshold: float = 0.50) -> frozenset[str]:
    """Токены из имён методов встречающиеся у >= threshold доли всех модулей → шум."""
    svc_toks: dict[str, set[str]] = {m: set() for m in all_modules}
    for node in sk["nodes"].values():
        if node.get("kind") != "method":
            continue
        mod = node.get("module", "")
        if mod not in svc_toks:
            continue
        name = node.get("name", "")
        if "<" in name:
            continue
        for tok in _tokenize_camel(name):
            svc_toks[mod].add(tok)
    return derive_high_freq_tokens(svc_toks, len(all_modules), threshold=threshold)


def _extract_auth_namespace_candidates(
    sk: dict, domain_tokens: set[str]
) -> list[str]:
    """Токены методов из auth/security/verif* сервисных узлов → кандидаты tier4.

    Portable: вместо namespace_filter="security" (только имя пакета) ищет по модулю,
    что захватывает auth- и verification- сервисы независимо от их package structure.
    """
    auth_mods: set[str] = set()
    all_modules = {n.get("module", "") for n in sk["nodes"].values() if n.get("module")}
    for mod in all_modules:
        low = mod.lower()
        if any(kw in low for kw in ("auth", "security", "verif", "jwt")):
            auth_mods.add(mod)

    from src._config import cfg_get as _cfg_get
    noise = frozenset(_cfg_get("auth_namespace_noise", [])) | _derive_method_noise(sk, all_modules)
    counts: Counter[str] = Counter()
    for node in sk["nodes"].values():
        if node.get("kind") != "method":
            continue
        mod = node.get("module", "")
        if mod not in auth_mods:
            continue
        name = node.get("name", "")
        if "<" in name:
            continue
        for tok in _tokenize_camel(name):
            if tok not in domain_tokens and tok not in noise:
                counts[tok] += 1
    return [tok for tok, _ in counts.most_common(20)]


def derive_tier4_remote_tokens(sk: dict, language: str, no_qwen: bool) -> list[str]:
    from src._config import cfg_get as _cfg_get
    domain_tokens = {n.get("module", "").removeprefix("ts-").removesuffix("-service")
                     .replace("-", "_") for n in sk["nodes"].values() if n.get("module")}
    auth_whitelist = frozenset(_cfg_get("auth_token_whitelist", []))
    candidates = _extract_auth_namespace_candidates(sk, domain_tokens)
    # Фильтр: оставить только токены из whitelist (из конфига)
    filtered = [t for t in candidates if t in auth_whitelist]
    return sorted(filtered) if filtered else sorted(auth_whitelist & set(candidates))


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
        modules, naming["prefix"], naming["suffix"], no_qwen=args.no_qwen, sk=sk
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

    # Шаг 5.5: infra_module_tokens — auto-derived из скелета + auto_categories
    from src._config import cfg_cache_dir as _cfg_cache_dir
    auto_cat_path = _cfg_cache_dir() / "auto_categories.json"
    print("Вычисляю infra_module_tokens...", file=sys.stderr)
    infra_toks = derive_infra_module_tokens(
        modules, naming["prefix"], naming["suffix"],
        opt_groups, auto_cat_path, no_qwen=args.no_qwen,
    )
    print(f"infra_module_tokens: {infra_toks}", file=sys.stderr)
    if infra_toks:
        config["infra_module_tokens"] = infra_toks

    # Сохранение
    from src._config import cfg_cache_dir
    out_path = Path(args.out) if args.out else cfg_cache_dir() / "project_config.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nСохранено: {out_path}", file=sys.stderr)
    print(json.dumps(config, ensure_ascii=False, indent=2))

    # Шаг 6: optional_patterns.json — regex для has_*/no_* ключей (step_typer.py)
    # Требует Qwen для корректной фильтрации optional groups.
    # При --no-qwen группы содержат мусор → шаг пропускается, кеш не перезаписывается.
    if args.no_qwen:
        print("Шаг 6 пропущен (--no-qwen): optional_patterns.json не перезаписан", file=sys.stderr)
        return

    # Источник токенов: opt_tokens_per_grp (из тегов паспортов + имён сервисов).
    # step_typer.py ожидает ключи: has_food, no_food, has_insurance, no_insurance,
    #   has_luggage, no_luggage (старый API: consign → luggage).
    # Маппинг group_name → pattern_key: базис hardcoded (известные домены),
    # незнакомые группы разрешает Qwen.
    _BASE_PATTERN_KEYS: dict[str, str] = {
        "food": "food", "meal": "food", "dining": "food",
        "insurance": "insurance", "assurance": "insurance", "coverage": "insurance",
        "consign": "luggage", "consignment": "luggage", "luggage": "luggage", "baggage": "luggage",
    }
    # Определить какие группы не покрыты базисом → передать в Qwen
    unknown_groups: list[str] = []
    for grp, tokens in opt_tokens_per_grp.items():
        pat_key = _BASE_PATTERN_KEYS.get(grp)
        if pat_key is None:
            pat_key = next((
                _BASE_PATTERN_KEYS[tok] for tok in tokens if tok in _BASE_PATTERN_KEYS
            ), None)
        if pat_key is None:
            unknown_groups.append(grp)

    # Qwen шаг 7: для незнакомых групп определить тип optional addon
    qwen_pattern_keys: dict[str, str] = {}
    if unknown_groups and not args.no_qwen:
        try:
            from src.localization.qwen_encoder import QwenEncoder
            enc7 = QwenEncoder()
            if enc7.enabled:
                known_keys = sorted({v for v in _BASE_PATTERN_KEYS.values()})
                grp_desc = "\n".join(
                    f"  {g}: services={opt_groups.get(g, [])}, tokens={opt_tokens_per_grp[g]}"
                    for g in unknown_groups
                )
                obj_schema2: dict = {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                }
                prompt7 = (
                    f"Optional service groups with unknown pattern key:\n{grp_desc}\n\n"
                    f"Known optional addon types: {known_keys}\n"
                    f"Map each group to the closest known type (food/insurance/luggage), "
                    f"or assign a new short snake_case key if it's a different addon type.\n"
                    f"Return ONLY JSON: {{\"group_name\": \"pattern_key\", ...}}"
                )
                raw7 = enc7._call_ollama(prompt7, schema=obj_schema2, num_predict=100, timeout=30)
                if isinstance(raw7, dict):
                    for g, k in raw7.items():
                        if g in unknown_groups and isinstance(k, str) and k:
                            qwen_pattern_keys[g] = k
                            print(f"  Qwen pattern_key: {g} → {k}", file=sys.stderr)
        except Exception as e:
            print(f"[WARN] Qwen ошибка step7 (pattern_keys): {e}", file=sys.stderr)

    patterns: dict[str, str] = {}
    for grp, tokens in opt_tokens_per_grp.items():
        # Определить pattern_key: базисный маппинг → Qwen → group_name as-is
        pat_key = _BASE_PATTERN_KEYS.get(grp)
        if pat_key is None:
            pat_key = next((
                _BASE_PATTERN_KEYS[tok] for tok in tokens if tok in _BASE_PATTERN_KEYS
            ), None)
        if pat_key is None:
            pat_key = qwen_pattern_keys.get(grp, grp)  # Qwen или fallback: group_name as-is
        # Добавить все синонимы pat_key из маппинга (luggage/baggage для consign)
        synonyms = {t for t, k in _BASE_PATTERN_KEYS.items() if k == pat_key and len(t) >= 4}
        all_toks = sorted(set(tokens) | synonyms)
        tok_pat = "|".join(t for t in all_toks if len(t) >= 4)
        if not tok_pat:
            continue
        # Strict has_* pattern: requires context word "with" or action verb.
        # Bare token match (e.g. just "assurance") causes FP when the step mentions
        # the feature negatively or in a generic context.
        patterns[f"has_{pat_key}"] = rf"\bwith\b.{{0,80}}\b({tok_pat})\b"
        patterns[f"no_{pat_key}"] = rf"\b(without|no)\b.{{0,80}}\b({tok_pat})\b"
    opt_patterns_path = cfg_cache_dir() / "optional_patterns.json"
    opt_patterns_path.write_text(json.dumps(patterns, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Сохранено: {opt_patterns_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
