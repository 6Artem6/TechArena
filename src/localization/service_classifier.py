"""Token Taxonomy — классификация сервисов по уровням токенов (P-93).

Реализует 4-уровневую классификацию по P-93:
  Tier 1 (системные):      if, return, class → не встречаются в именах сервисов, игнорируем
  Tier 2 (служебные):      service, controller, dto, repository → arch_role (фильтруем)
  Tier 3 (доменные):       ticket, order, cancel → **авто-извлекаются** из имён сервисов скелета
  Tier 4 (инфраструктурные): auth, verify, jwt → infra_tags (небольшой стабильный набор)

Tier 3 НЕ захардкожен — выводится из репозитория:
  Tier 3 = токены сервисных имён − Tier 2 (арх-роли) − Tier 4 (инфра)
Это делает классификатор переносимым: не нужен ручной доменный словарь.

Tier 2 тоже может авто-обнаруживаться: CamelCase суффиксы классов ≥ 3 вхождений.
Вызвать `precompute_tier2_from_index(index)` при загрузке скелета.

Два уровня строгости (P-93):
  - detect_infra_remote(): широкий Tier 4 → для фильтра remote edges
  - detect_infra_executes(): строгий Tier 4 → для фильтра executes

P-93 offline: build_service_profiles(modules, index) → per-service domain vocabulary
P-93 online:  используется в searcher.py для augmentation BM25-запроса
"""
from __future__ import annotations

import re
from collections import Counter

from .._config import cfg_get
from ..graph.siblings import _strip as _svc_strip


# ─── Tier 4: инфраструктурные токены (захардкожены — стабильные для веб-приложений) ───

def _get_tier4_remote() -> frozenset[str]:
    lst = cfg_get("tier4_remote_tokens", [
        "auth", "authentication", "authorize", "authorization",
        "verify", "verification",
        "jwt", "token", "credential", "password", "secret", "captcha",
        "user", "account",
    ])
    return frozenset(lst)


def _get_tier4_executes() -> frozenset[str]:
    lst = cfg_get("tier4_executes_tokens", [
        "verify", "verification", "code", "captcha",
    ])
    return frozenset(lst)


# ─── Tier 2: архитектурные роли (фреймворко-специфичные, не доменные) ─────────

def _get_tier2_arch() -> frozenset[str]:
    lst = cfg_get("tier2_arch_tokens", [
        "service", "controller", "dto", "repository", "impl",
        "api", "server", "client", "handler", "manager",
        "factory", "helper", "util", "utils",
    ])
    return frozenset(lst)


# ─── Ленивый кеш (инициализируется один раз на процесс) ────────────────────────

_TIER4_REMOTE_CACHE:   frozenset[str] | None = None
_TIER4_EXECUTES_CACHE: frozenset[str] | None = None
_TIER2_ARCH_CACHE:     frozenset[str] | None = None


def _tier4_remote() -> frozenset[str]:
    global _TIER4_REMOTE_CACHE
    if _TIER4_REMOTE_CACHE is None:
        _TIER4_REMOTE_CACHE = _get_tier4_remote()
    return _TIER4_REMOTE_CACHE


def _tier4_executes() -> frozenset[str]:
    global _TIER4_EXECUTES_CACHE
    if _TIER4_EXECUTES_CACHE is None:
        _TIER4_EXECUTES_CACHE = _get_tier4_executes()
    return _TIER4_EXECUTES_CACHE


def _tier2_arch() -> frozenset[str]:
    global _TIER2_ARCH_CACHE
    if _TIER2_ARCH_CACHE is None:
        _TIER2_ARCH_CACHE = _get_tier2_arch()
    return _TIER2_ARCH_CACHE


# ─── Tier 3: авто-извлечение из репозитория (P-93) ─────────────────────────────

def _extract_tier3(modules: set[str]) -> frozenset[str]:
    """Авто-извлечь Tier 3 (доменные токены) из имён сервисов скелета (P-93).

    Tier 3 = все токены сервисных имён − Tier 2 (арх-роли) − Tier 4 (инфра).
    Не требует ручного словаря — переносимо на любой репозиторий.

    Примеры (train-ticket):
      ts-order-service   → 'order'        ✓ domain
      ts-auth-service    → 'auth'         → Tier 4 remote, исключён → пусто
      ts-route-plan-service → 'route','plan'  ✓ domain
    """
    t2 = _tier2_arch()
    t4 = _tier4_remote() | _tier4_executes()
    tokens: set[str] = set()
    for name in modules:
        for tok in _svc_strip(name).split("-"):
            if tok and tok not in t2 and tok not in t4:
                tokens.add(tok)
    return frozenset(tokens)


# ─── Классификатор инфра-сервисов ─────────────────────────────────────────────

def _is_infra(name: str, tier4: frozenset[str], tier3: frozenset[str]) -> bool:
    """Сервис является infra если имеет Tier-4 токены и не имеет Tier-3 токенов.

    admin-* сервисы никогда не являются infra (это UI-интерфейсы управления).
    """
    s = _svc_strip(name)
    if s.startswith("admin-"):
        return False
    tokens = set(s.split("-"))
    return bool(tokens & tier4) and not bool(tokens & tier3)


# ─── P-93: Авто-обнаружение Tier 2 из скелета ──────────────────────────────────

def precompute_tier2_from_index(index) -> None:
    """Авто-обнаружить Tier 2 из CamelCase суффиксов классов скелета (P-93).

    Токены встречающиеся как суффикс имени класса ≥ 3 раз → Tier 2 арх-роль.
    Результат объединяется с YAML tier2_arch_tokens (дополнение, не замена).
    Вызывать из predict._build_index() один раз при загрузке скелета.
    """
    global _TIER2_ARCH_CACHE
    suffix_count: Counter[str] = Counter()
    for nid, node in index.N.items():
        if node.get("kind") not in ("class", "interface"):
            continue
        name: str = node.get("name", "")
        parts = re.sub(r"([a-z])([A-Z])", r"\1 \2", name).split()
        if parts:
            suffix_count[parts[-1].lower()] += 1

    t4 = _tier4_remote() | _tier4_executes()
    auto_t2 = frozenset(
        tok for tok, cnt in suffix_count.items()
        if cnt >= 3 and tok not in t4
    )
    _TIER2_ARCH_CACHE = _get_tier2_arch() | auto_t2


# ─── P-93: Профили сервисов для domain matching ─────────────────────────────────

_API_PATH_NOISE = frozenset({"api", "v1", "v2", "v3", "http", "https", "get", "post"})


def build_service_profiles(modules: set[str], index) -> dict[str, frozenset[str]]:
    """P-93 offline: построить доменный профиль каждого сервиса.

    Профиль = Tier 3 токены из:
      - имени сервиса (уже в _extract_tier3)
      - путей endpoint'ов: /travelservice/trips/left → {'travelservice', 'trips', 'left'}
      - имён классов без арх-суффиксов: TravelServiceImpl → {'travel'}

    Online-использование (searcher.py):
      query_tokens → best-matching profile → augment BM25 query with profile tokens
    """
    tier2 = _tier2_arch()
    tier4 = _tier4_remote() | _tier4_executes()

    module_nodes: dict[str, list[dict]] = {}
    for nid, node in index.N.items():
        mod = index.module.get(nid, "")
        if mod:
            module_nodes.setdefault(mod, []).append(node)

    profiles: dict[str, frozenset[str]] = {}
    for mod in modules:
        tokens: set[str] = set()

        # Из имени сервиса (Tier 3)
        for tok in _svc_strip(mod).split("-"):
            if tok and tok not in tier2 and tok not in tier4:
                tokens.add(tok)

        for node in module_nodes.get(mod, []):
            kind = node.get("kind", "")

            # Endpoint paths: /api/v1/travelservice/trips/left → endpoint tokens
            if kind in ("endpoint", "method"):
                path = node.get("url", "") or node.get("path", "")
                if path:
                    for part in path.lower().split("/"):
                        part = part.strip().rstrip("{}")
                        if len(part) > 2 and part not in _API_PATH_NOISE and part not in tier4:
                            tokens.add(part)

            # Class names без арх-ролей: "TravelServiceImpl" → {'travel'}
            if kind in ("class", "interface"):
                name: str = node.get("name", "")
                parts = re.sub(r"([a-z])([A-Z])", r"\1 \2", name).lower().split()
                for part in parts:
                    if part and part not in tier2 and part not in tier4 and len(part) > 2:
                        tokens.add(part)

        profiles[mod] = frozenset(tokens)

    return profiles


# ─── Публичный API ─────────────────────────────────────────────────────────────

def detect_infra_remote(modules: set[str]) -> frozenset[str]:
    """Инфра-сервисы для фильтрации remote edges (широкий Tier 4).

    Tier 3 авто-извлекается из modules — ручного словаря не требуется.
    """
    tier4 = _tier4_remote()
    tier3 = _extract_tier3(modules)
    return frozenset(m for m in modules if _is_infra(m, tier4, tier3))


def detect_infra_executes(modules: set[str]) -> frozenset[str]:
    """Инфра-сервисы для фильтрации executes (строгий Tier 4).

    Tier 3 авто-извлекается из modules — ручного словаря не требуется.
    """
    tier4 = _tier4_executes()
    tier3 = _extract_tier3(modules)
    return frozenset(m for m in modules if _is_infra(m, tier4, tier3))
