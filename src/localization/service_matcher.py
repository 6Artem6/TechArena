"""P-93/P-106: Service-level BM25 matcher по коду репозитория.

Строит bag-of-words каждого сервиса из двух источников кода:
  1. CamelCase-токены из скелета (метод/класс/поле/параметр имена)
  2. @RequestMapping/@GetMapping пути из Java-источников в targets/

Не требует project-specific vocabulary — полностью авто-выводится из кода.

Заменяет доменную часть _TOKEN_MAP: вместо хардкода "search_train → [trips, travel]"
находит ts-travel-service потому что:
  - код содержит "travel", "trip", "train"
  - endpoint "/api/v1/travelservice/trips/left" содержит "travelservice", "trips"

Использование:
    matcher = ServiceMatcher(set(ix.module.values()), ix, root=TARGETS)
    extra = matcher.augment(query_tokens)  # domain tokens лучшего сервиса-кандидата
"""
from __future__ import annotations

import re
from pathlib import Path

from ..core.bm25 import BM25 as _BM25Base

# Шумовые Java/Spring-токены — универсально высокочастотные, не несут доменного смысла.
# Все сервисы имеют main(), Spring-бины, Builder-паттерн → низкий IDF, нужно удалять явно.
_CODE_NOISE = frozenset([
    # Spring / Java boilerplate
    "http", "headers", "header", "response", "impl", "get", "set", "by",
    "string", "void", "rest", "config", "cors", "entity", "id", "ids",
    "controller", "service", "repository", "all", "init", "application",
    "security", "template", "request", "result", "data", "type", "name",
    "enable", "web", "null", "true", "false", "send",
    "object", "integer", "boolean", "long", "double", "float",
    "date", "time", "is", "has", "can", "with",
    # Паттерны и утилиты (есть в каждом Spring-приложении)
    "main", "builder", "encoder", "password", "configurer", "configure",
    "mapping", "mappings", "register", "addcors", "cors",
    # CRUDные глаголы — низкий IDF (во всех сервисах)
    "new", "map", "list", "add", "find", "delete", "update", "create",
    "query", "search", "save", "exist", "exists", "count", "load", "read",
    "write", "check", "batch",
])

# CamelCase-разбивка: OrderServiceImpl → ["order", "service", "impl"]
_CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def _split(s: str) -> list[str]:
    """CamelCase + underscore split → нижний регистр, len ≥ 2."""
    parts: list[str] = []
    for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", s):
        for p in (_CAMEL_RE.findall(w) if "_" not in w else w.split("_")):
            p = p.lower()
            if len(p) >= 2:
                parts.append(p)
    return parts


# @RequestMapping / @GetMapping / @PostMapping etc. → извлечь путь из строки аннотации
_MAPPING_RE = re.compile(
    r'@(?:Request|Get|Post|Put|Delete|Patch)Mapping\s*\('
    r'\s*(?:value\s*=\s*|path\s*=\s*)?["\']([^"\']+)["\']',
    re.I,
)
# Шум в URL-путях (не несут доменного смысла)
_PATH_NOISE = frozenset(["api", "v1", "v2", "v3", "http", "https"])


def _extract_endpoint_tokens(root: Path, module: str) -> list[str]:
    """Статически извлечь токены из @RequestMapping путей Java-источников.

    Сканирует все Java-файлы сервиса в targets/ через regex аннотаций.
    Пример: @GetMapping("/trips/left") → ["trips", "left"]
    """
    svc_root = root / module
    if not svc_root.is_dir():
        return []

    tokens: list[str] = []
    for jf in svc_root.rglob("*.java"):
        # Пропускаем тесты
        if "/test/" in jf.as_posix():
            continue
        try:
            src = jf.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in _MAPPING_RE.finditer(src):
            path = m.group(1)
            for part in path.split("/"):
                part = part.strip().rstrip("{}")
                if len(part) > 2 and part not in _PATH_NOISE:
                    # Сплит по _ и camelCase
                    tokens.extend(_split(part))
    return tokens


def build_service_docs(
    modules: set[str],
    index,
    root: "Path | None" = None,
) -> dict[str, list[str]]:
    """Построить bag-of-words каждого сервиса из кода.

    Источники токенов:
      1. CamelCase split из скелета: метод/класс/поле/параметр имена
      2. @RequestMapping/@GetMapping пути из Java-источников в targets/ (если root задан)

    Фильтрует шумовые Java-токены (_CODE_NOISE) + Tier 2/4 из Token Taxonomy.
    Исключает ts-common (shared library, не доменный сервис).
    """
    from src.localization.service_classifier import _tier2_arch, _tier4_remote, _tier4_executes
    noise = _CODE_NOISE | _tier2_arch() | _tier4_remote() | _tier4_executes()

    # Исключаем общие/инфра модули из domain matching (не являются entry-point кандидатами)
    _SKIP_MODULES = frozenset(["ts-common"])
    docs: dict[str, list[str]] = {mod: [] for mod in modules if mod not in _SKIP_MODULES}

    # 1. Токены из скелета (index.N)
    for nid, node in index.N.items():
        mod = index.module.get(nid, "")
        if mod not in docs:
            continue

        tokens: list[str] = []

        name = node.get("name") or ""
        if name and not name.startswith("<"):
            tokens.extend(_split(name))

        owner = node.get("owner") or ""
        if owner:
            tokens.extend(_split(owner.split(".")[-1].split("#")[0]))

        for field in ("descriptor", "return"):
            val = node.get(field) or ""
            for part in re.split(r"[,\s<>()[\];]+", val):
                tokens.extend(_split(part.split(".")[-1]))

        docs[mod].extend(t for t in tokens if t not in noise and len(t) >= 2)

    # 2. Endpoint path tokens из Java-источников (статический анализ аннотаций)
    if root is not None:
        for mod in docs:
            ep_tokens = _extract_endpoint_tokens(root, mod)
            docs[mod].extend(t for t in ep_tokens if t not in noise and len(t) >= 2)

    return docs


def _expand_plurals(tokens: list[str]) -> list[str]:
    """Добавить десуффиксированные формы для английских множественных чисел.

    "trains" → добавить "train" (len ≥ 5, окончание "s", не "ss").
    Расширение аддитивно — оригинальный токен сохраняется.
    Результирующие формы без словаря: если "proces" нет в документах — BM25 score = 0.
    """
    result = list(tokens)
    for t in tokens:
        if len(t) >= 5 and t.endswith("s") and not t.endswith("ss"):
            result.append(t[:-1])
    return result


class ServiceMatcher:
    """BM25 matcher по bag-of-words сервисов из кода скелета (P-93/P-106).

    Не требует project vocabulary.
    Для запроса возвращает токены кода наиболее релевантного сервиса —
    они добавляются к BM25-запросу в searcher.py для точного поиска handler'а.
    """

    def __init__(self, modules: set[str], index, root: "Path | None" = None):
        self._docs = build_service_docs(modules, index, root=root)
        # BM25 с именами модулей как ключами (dict-интерфейс src.core.bm25.BM25)
        corpus = {
            m: (self._docs[m] if self._docs[m] else ["_empty_"])
            for m in self._docs
        }
        self._bm25 = _BM25Base(corpus)

    def rank(self, query: list[str], top_k: int = 3) -> list[tuple[str, float]]:
        """Ранжировать сервисы по BM25 для запроса (list of tokens).

        Расширяет запрос десуффиксацией английских множественных чисел.
        Возвращает [(module, score)] — только ненулевые баллы, top_k.
        """
        results = self._bm25.rank(_expand_plurals(query))
        # BM25.rank() → [(score, id)]; перевернуть в (module, score)
        return [(mod, sc) for sc, mod in results[:top_k]]

    def augment(self, query: list[str], min_score: float = 0.5, idf_percentile: float = 0.6) -> list[str]:
        """P-93 online: добавить характерные токены лучшего сервиса к BM25-запросу.

        Возвращает уникальные токены с IDF выше idf_percentile-квантиля —
        токены, встречающиеся в небольшом количестве сервисов (характерные).
        Аддитивно — не заменяет, не удаляет уже имеющиеся токены.
        """
        top = self.rank(query, top_k=1)
        if not top:
            return []
        best_mod, best_score = top[0]
        if best_score < min_score:
            return []

        # Порог IDF: возвращать токены встречающиеся в ≤ 40% сервисов
        sorted_idfs = sorted(self._bm25.idf.values())
        if not sorted_idfs:
            return []
        idf_threshold = sorted_idfs[int(len(sorted_idfs) * idf_percentile)]

        q_set = set(_expand_plurals(query))
        doc = self._docs.get(best_mod, [])
        # Уникальные токены с высоким IDF (характерные для сервиса)
        seen: set[str] = set()
        result: list[str] = []
        for t in doc:
            if t in q_set or t in seen:
                continue
            seen.add(t)
            if self._bm25.idf.get(t, 0.0) >= idf_threshold:
                result.append(t)
        return result
