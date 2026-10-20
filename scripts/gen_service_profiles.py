"""Оффлайн-генерация ServiceProfile для каждого сервиса.

Запуск: python scripts/gen_service_profiles.py [--force-profiles] [--force-passports]
         python scripts/gen_service_profiles.py --tags   (только теги через Qwen 1.5b)

Архитектура (минимум Qwen-вызовов):
  1. Паспорта (Qwen per controller file) → бизнес-описания методов
  2. Профили (алгоритмически из паспортов) → 0 доп. Qwen-вызовов
  3. Теги сервисов (Qwen 1.5b) → ServiceProfile.tags + cache/project_profile.json

  Passport cache key: filename:md5hash → {method: {business_summary, synonyms}}
  Profile cache:      service_name → ServiceProfile
  Project profile:    cache/project_profile.json  (домен + общие теги системы)

Если паспортных данных нет (сервис не в skeleton.handlers) — fallback на Java-методам.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

from src.localization.service_profiles import ServiceProfile, _CACHE_KEY_VERSION

SKEL_PATH     = ROOT / "tt-challenge" / "skeleton" / "trainticket.json"
TARGETS_DIR   = ROOT / "tt-challenge" / "targets" / "trainticket"
PROFILE_CACHE = ROOT / "cache" / "service_profiles_v1.pkl"

# Стоп-слова для BM25: слишком универсальные чтобы различать сервисы
_STOP = frozenset({
    "the", "for", "with", "this", "that", "from", "into", "and", "all", "any",
    "are", "was", "has", "can", "its", "not", "get", "set", "add", "new",
    "list", "find", "query", "return", "service", "request", "response",
    "account", "user", "data", "info", "http", "api", "result", "status",
    "train", "ticket", "order", "system",  # слишком частые во всех сервисах
})

# Параметры бронирования: детектируем по ключевым словам в summaries/synonyms
_PARAM_HINTS = {
    "food":      re.compile(r"\b(food|meal|catering|restaurant)\b", re.I),
    "insurance": re.compile(r"\b(insurance|assurance|coverage)\b", re.I),
    "luggage":   re.compile(r"\b(luggage|consign|baggage)\b", re.I),
    "seat_class": re.compile(r"\b(seat|class|first.?class|business.?class)\b", re.I),
    "train_type": re.compile(r"\b(gaotie|high.?speed|regular|bullet)\b", re.I),
}


# ---------------------------------------------------------------------------
# Шаг 1 — паспорта (делегируем в passport_generator.py)
# ---------------------------------------------------------------------------

def _run_passport_generation(
    ix,
    qwen,
    force: bool = False,
) -> dict[str, dict]:
    """Сгенерировать/загрузить паспорта и вернуть raw cache {filename:hash: {method: ...}}."""
    import pickle
    from src.localization.passport_generator import _CACHE_FILE, load_or_generate_passports

    if force and _CACHE_FILE.exists():
        _CACHE_FILE.unlink()

    # load_or_generate_passports возвращает {nid: PassportData} — нам нужен raw cache
    load_or_generate_passports(ix, TARGETS_DIR, qwen, n_workers=4)

    if _CACHE_FILE.exists():
        return pickle.loads(_CACHE_FILE.read_bytes())
    return {}


# ---------------------------------------------------------------------------
# Шаг 2 — маппинг паспортов к сервисам
# ---------------------------------------------------------------------------

def _map_passports_to_services(
    passport_cache: dict,
    ix,
) -> dict[str, dict[str, dict]]:
    """Построить {service_name: {method_name: {business_summary, synonyms}}}."""
    # nid → service name через ix.N
    nid_to_svc: dict[str, str] = {}
    for nid, node in ix.N.items():
        mod = node.get("module", "")
        if mod:
            nid_to_svc[nid] = mod

    # handler → (service, method_name)
    handler_info: dict[str, tuple[str, str]] = {}
    for nid, _verb, _tpl in ix.handlers:
        n = ix.N.get(nid, {})
        svc = n.get("module", "")
        mname = n.get("name", "")
        fname = Path(n.get("file", "")).name
        if svc and mname and fname:
            handler_info[f"{fname}::{mname}"] = (svc, mname)

    by_service: dict[str, dict[str, dict]] = {}
    for cache_key, methods in passport_cache.items():
        fname = cache_key.split(":")[0]
        for mname, ep_data in methods.items():
            if mname == "home":
                continue
            key = f"{fname}::{mname}"
            if key in handler_info:
                svc, _ = handler_info[key]
                by_service.setdefault(svc, {})[mname] = ep_data

    return by_service


# ---------------------------------------------------------------------------
# Шаг 3 — алгоритмическая деривация профиля из паспортов
# ---------------------------------------------------------------------------

def _camel_split(name: str) -> list[str]:
    """'createNewOrder' → ['create', 'new', 'order']"""
    return re.findall(r"[a-z]+", re.sub(r"([A-Z])", r" \1", name).lower())


def _derive_profile(
    module_name: str,
    methods: dict[str, dict],
) -> ServiceProfile:
    """Алгоритмически вывести профиль из паспортных данных (0 Qwen-вызовов)."""
    stripped = module_name.replace("ts-", "").replace("-service", "")
    tag = stripped.split("-")[0]

    # vector_search_phrases: топ бизнес-описания (уже NL-качество от Qwen)
    phrases = [
        ep.get("business_summary", "").strip()
        for ep in methods.values()
        if ep.get("business_summary", "").strip()
    ][:4]

    # bm25_keywords: из имён методов (camelCase split)
    method_toks: list[str] = []
    for mname in methods:
        method_toks.extend(t for t in _camel_split(mname) if t not in _STOP and len(t) > 2)

    # Дополняем терминами из summaries
    summary_toks: list[str] = []
    for ep in methods.values():
        summ = ep.get("business_summary", "").lower()
        summary_toks.extend(
            t for t in re.findall(r"\b[a-z]{3,}\b", summ)
            if t not in _STOP
        )

    # TF-ранг: слова из имён методов надёжнее слов из summaries
    kw_freq = Counter(method_toks)
    summ_freq = Counter(summary_toks)
    seen: set[str] = set()
    kws: list[str] = []
    for t, _ in kw_freq.most_common(4):
        if t not in seen:
            kws.append(t)
            seen.add(t)
    for t, _ in summ_freq.most_common(5):
        if t not in seen and len(kws) < 6:
            kws.append(t)
            seen.add(t)

    # handled_parameters: по ключевым словам в summaries + synonyms
    all_text = " ".join(
        ep.get("business_summary", "") + " " + " ".join(ep.get("synonyms", []))
        for ep in methods.values()
    )
    params = [p for p, pat in _PARAM_HINTS.items() if pat.search(all_text)]

    return ServiceProfile(
        service_name=module_name,
        one_word_tag=tag,
        bm25_keywords=kws[:6],
        vector_search_phrases=phrases,
        handled_parameters=params,
    )


# ---------------------------------------------------------------------------
# Fallback: Java-методы для сервисов без паспортных данных
# ---------------------------------------------------------------------------

def _java_fallback_profile(module_name: str) -> ServiceProfile:
    """Профиль из имён Java-методов и скелет-графа (когда паспортов нет)."""
    stripped = module_name.replace("ts-", "").replace("-service", "")
    tag = stripped.split("-")[0]

    svc_dir = TARGETS_DIR / module_name
    methods: list[str] = []
    if svc_dir.exists():
        _skip = frozenset({"main", "home", "configure", "corsConfigurer", "passwordEncoder"})
        for java_file in svc_dir.rglob("*.java"):
            if "test" in str(java_file).lower():
                continue
            # Пропускаем Application.java и config/ — только бизнес-логика
            if "Application.java" in java_file.name or "config" in str(java_file).lower():
                continue
            try:
                content = java_file.read_text(errors="replace")
            except Exception:
                continue
            # Методы с @GetMapping/@PostMapping (endpoint handlers)
            file_methods: list[str] = []
            for m in re.finditer(
                r'@(?:Get|Post|Put|Delete|Request)Mapping\([^)]*\)[^{]*?'
                r'public\s+\w[\w<>,.?\[\] ]+\s+(\w+)\s*\(',
                content,
            ):
                nm = m.group(1)
                if nm not in _skip:
                    file_methods.append(nm)
            # Fallback per-file: public methods (только если в файле нет endpoint-ов)
            if not file_methods:
                for m in re.finditer(r'public\s+\w[\w<>,.?\[\] ]+\s+(\w+)\s*\(', content):
                    nm = m.group(1)
                    if nm not in _skip and len(nm) > 3:
                        file_methods.append(nm)
            methods.extend(file_methods)

    kws: list[str] = [
        t for mname in list(dict.fromkeys(methods))[:8]
        for t in _camel_split(mname)
        if t not in _STOP and len(t) > 2
    ]
    kw_top = [t for t, _ in Counter(kws).most_common(6)]

    parts = stripped.replace("-", " ").split()
    phrases = [
        f"{stripped} operation",
        f"manage {stripped}",
    ]
    if kw_top:
        phrases.append(f"{' '.join(parts)}: " + ", ".join(kw_top[:3]))

    return ServiceProfile(
        service_name=module_name,
        one_word_tag=tag,
        bm25_keywords=kw_top,
        vector_search_phrases=phrases,
        handled_parameters=[],
    )


# ---------------------------------------------------------------------------
# Лёгкие теги через Qwen 1.5b (офлайн)
# ---------------------------------------------------------------------------

_LITE_MODEL   = "qwen2.5-coder:1.5b"
_OLLAMA_URL   = "http://localhost:11434/api/generate"
_PROJECT_PROFILE_PATH = ROOT / "cache" / "project_profile.json"

_TAG_SYSTEM = (
    "You are a concise API capability classifier. "
    "Reply with ONLY a comma-separated list of 4-7 short English words or hyphenated phrases. "
    "Tags must cover both technical terms AND user-facing query types (what a user would search for). "
    "Include synonyms. No explanations, no punctuation other than commas and hyphens."
)

_TAG_SVC_TMPL = (
    "Service: {name}\n"
    "Key methods: {methods}\n"
    "What it does: {summary}\n"
    "Generate tags covering: the service domain, query types it handles, "
    "and user-facing synonyms for its capabilities. Tags:"
)

# Для plan-сервисов: спросить какие step-паттерны они обрабатывают
_TAG_PLAN_TMPL = (
    "Service {name} handles ADVANCED/COMPLEX search queries. "
    "Methods: {methods}. Descriptions: {summary}.\n"
    "List 5-8 short English words a USER might use when making this kind of search "
    "(e.g. transit, transfer, connecting, multi-leg, advanced, cheapest, fastest). "
    "Focus on USER vocabulary, not technical terms. Tags:"
)

_TAG_PROJECT_TMPL = (
    "Services in this system: {services}\n"
    "What is the domain of this system? "
    "Reply with 3-5 comma-separated domain tags. Tags:"
)


def _ollama_tags(prompt: str, model: str = _LITE_MODEL, timeout: int = 30) -> list[str]:
    """Вызвать Ollama с простым текстовым промптом, вернуть список тегов."""
    payload = json.dumps({
        "model":  model,
        "system": _TAG_SYSTEM,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0, "num_predict": 64},
    }).encode()
    try:
        req = Request(_OLLAMA_URL, data=payload, headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=timeout) as resp:
            raw = json.loads(resp.read()).get("response", "")
        # Парсим comma-separated список
        tags = [t.strip().lower().replace(" ", "-") for t in raw.split(",") if t.strip()]
        return [t for t in tags if 2 <= len(t) <= 40][:6]
    except Exception as e:
        print(f"  [tags] ollama error: {e}", file=sys.stderr)
        return []


def _ollama_available(model: str = _LITE_MODEL) -> bool:
    try:
        with urlopen(Request("http://localhost:11434/api/tags"), timeout=3) as r:
            names = {m["name"] for m in json.loads(r.read()).get("models", [])}
        return model in names or any(model.split(":")[0] in n for n in names)
    except Exception:
        return False


def generate_tags(profiles: dict, force: bool = False) -> dict:
    """Сгенерировать теги для сервисов и project-level профиль через Qwen 1.5b.

    Обновляет profile.tags in-place. Сохраняет project_profile.json.
    """
    if not _ollama_available():
        print("[tags] Qwen 1.5b недоступен — теги пропущены", file=sys.stderr)
        return profiles

    print(f"\n[tags] Генерация тегов сервисов (модель={_LITE_MODEL})...")
    for mod, prof in profiles.items():
        if not force and getattr(prof, "tags", None):
            continue
        methods_str = ", ".join(prof.bm25_keywords[:8])
        phrases = getattr(prof, "vector_search_phrases", [])
        summary_str = ". ".join(phrases[:3]) if phrases else mod.replace("ts-", "").replace("-", " ")
        if "plan" in mod:
            prompt = _TAG_PLAN_TMPL.format(
                name=mod, methods=methods_str, summary=summary_str,
            )
        else:
            prompt = _TAG_SVC_TMPL.format(
                name=mod, methods=methods_str, summary=summary_str,
            )
        tags = _ollama_tags(prompt)
        prof.tags = tags
        print(f"  {mod}: {tags}")

    # Project-level профиль
    svc_sample = ", ".join(list(profiles)[:20])
    proj_tags = _ollama_tags(_TAG_PROJECT_TMPL.format(services=svc_sample))
    project_profile = {
        "tags": proj_tags,
        "service_count": len(profiles),
        "services": list(profiles.keys()),
    }
    _PROJECT_PROFILE_PATH.parent.mkdir(exist_ok=True)
    _PROJECT_PROFILE_PATH.write_text(json.dumps(project_profile, ensure_ascii=False, indent=2))
    print(f"[tags] project profile: {proj_tags} → {_PROJECT_PROFILE_PATH}")

    return profiles


# ---------------------------------------------------------------------------
# Основной цикл
# ---------------------------------------------------------------------------

def generate_profiles(
    force_profiles: bool = False,
    force_passports: bool = False,
) -> dict[str, ServiceProfile]:
    import pickle
    from src.localization.qwen_encoder import QwenEncoder
    from src.core.skeleton_index import Index

    skel = json.loads(SKEL_PATH.read_text())
    ix = Index(skel)
    modules = sorted(n["name"] for n in skel["nodes"].values() if n.get("kind") == "module")
    print(f"Сервисов в скелете: {len(modules)}")

    encoder = QwenEncoder()

    # --- Шаг 1: паспорта ---
    if encoder.enabled:
        print("\n[1/2] Генерация/загрузка паспортов...")
        passport_cache = _run_passport_generation(ix, encoder, force=force_passports)
    else:
        # Загрузить из существующего кеша
        import pickle as _pkl
        from src.localization.passport_generator import _CACHE_FILE
        if _CACHE_FILE.exists():
            passport_cache = _pkl.loads(_CACHE_FILE.read_bytes())
        else:
            passport_cache = {}
        print(f"[1/2] Qwen недоступен, загружен кеш паспортов ({len(passport_cache)} файлов)")

    methods_by_svc = _map_passports_to_services(passport_cache, ix)
    print(f"       Паспорта покрывают: {len(methods_by_svc)} сервисов")

    # --- Шаг 2: профили (алгоритмически) ---
    print("\n[2/2] Деривация профилей (без доп. Qwen-вызовов)...")

    # Загрузить существующий кеш профилей
    profiles: dict[str, ServiceProfile] = {}
    if PROFILE_CACHE.exists() and not force_profiles:
        with open(PROFILE_CACHE, "rb") as f:
            profiles = pickle.load(f)
        print(f"       Уже в кеше: {len(profiles)}")

    for mod in modules:
        if mod in profiles and not force_profiles:
            continue

        methods = methods_by_svc.get(mod, {})
        if methods:
            profile = _derive_profile(mod, methods)
            source = f"passport ({len(methods)} methods)"
        else:
            profile = _java_fallback_profile(mod)
            source = "java+skeleton fallback"

        profiles[mod] = profile
        print(f"  [{mod}]")
        print(f"    source={source}  tag={profile.one_word_tag!r}")
        print(f"    kw={profile.bm25_keywords}")
        if profile.vector_search_phrases:
            print(f"    phrases[0]={profile.vector_search_phrases[0]!r}")

    PROFILE_CACHE.parent.mkdir(exist_ok=True)
    with open(PROFILE_CACHE, "wb") as f:
        pickle.dump(profiles, f)
    print(f"\nГотово: {len(profiles)} профилей → {PROFILE_CACHE}")
    return profiles


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate service profiles from passports")
    parser.add_argument("--force-profiles",  action="store_true", help="Regenerate all profiles")
    parser.add_argument("--force-passports", action="store_true", help="Regenerate all passports too")
    parser.add_argument("--tags",            action="store_true", help="Generate service tags via Qwen 1.5b")
    parser.add_argument("--force-tags",      action="store_true", help="Regenerate tags even if present")
    args = parser.parse_args()

    _profiles = generate_profiles(
        force_profiles=args.force_profiles,
        force_passports=args.force_passports,
    )

    if args.tags or args.force_tags:
        import pickle as _pkl
        _profiles = generate_tags(_profiles, force=args.force_tags)
        with open(PROFILE_CACHE, "wb") as _f:
            _pkl.dump(_profiles, _f)
        print(f"Теги сохранены → {PROFILE_CACHE}")
