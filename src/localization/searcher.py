"""Фаза 2.2 — Улучшенный entry point поиск.

enhanced_ground() — замена baseline_nlp.ground():
  1. Классифицирует каждый шаг через step_typer → extra_tokens для BM25
  2. Добавляет step_type-токены к BM25-запросу (P-82)
  3. При travel/travel2, order/orderOther в топ-кандидатах выбирает
     правильный сервис по train_type из текста шага (P-75, P-54)

Авто-обнаружение сиблингов (P-75): rule-based (суффикс -other, числовой)
+ trigram cosine ≥ 0.70 — без хардкода имён сервисов.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_HERE = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_HERE))

from src.core.nlp import expand, query_toks, ADMIN, toks  # noqa: E402
from src.localization.step_typer import classify  # noqa: E402
from src.localization.service_matcher import ServiceMatcher  # noqa: E402
from src.graph.siblings import detect_all_siblings, _strip  # noqa: E402
from src.trace.oracle import Oracle  # noqa: E402
from src.similarity.encoder import SemanticReranker  # noqa: E402


# Типы шагов, которые наследуют train_type из предыдущего шага через oracle carry-over.
# "search_train" устанавливает train_type, эти шаги используют его если не переопределили сами.
_STEP_TYPES_USE_CONTEXT = frozenset({
    "book", "pay", "collect", "cancel", "rebook",
    "view_trip", "query_food_trip",
    "add_consign", "query_consign", "change_seat",
})


# ---------------------------------------------------------------------------
# Сиблинг-пары (P-75) — вычисляются из index при первом вызове
# ---------------------------------------------------------------------------

# Кеш: module → (sibling_module, is_g_side)
# G-сторона = без суффикса -other и без цифры (например, ts-order-service)
# OTHER-сторона = с суффиксом -other или цифрой (ts-order-other-service, ts-travel2-service)
_SIBLING_MAP: dict[str, tuple[str, bool]] | None = None  # module → (sibling, is_g_side)

# P-93/P-106: ServiceMatcher — BM25 по bag-of-words из кода сервисов
_SERVICE_MATCHER: ServiceMatcher | None = None
# Путь к targets/ (корень исходников). Устанавливается из predict._build_index().
_TARGETS_ROOT: "Path | None" = None

# SemanticReranker — BGE-M3 для параллельного сравнения с BM25 (не заменяет)
_RERANKER: "SemanticReranker | None" = None
# Текстовые описания обработчиков для dense-кодирования {handler_id → text}
_DOC_TEXTS: "dict[str, str] | None" = None

# Корпус BM25 (code + ui) — передаётся из predict._build_index() для обогащения doc_texts
_CODE_DOCS: "dict[str, list[str]] | None" = None
_UI_DOCS:   "dict[str, list[str]] | None" = None

# Токены без семантики: java/инфра термины, стоп-слова для doc_texts BGE-M3
_DOC_STOPWORDS: frozenset[str] = frozenset({
    "null", "fail", "false", "true", "error", "empty", "get", "set",
    "new", "from", "for", "not", "and", "something", "log", "logger",
    "return", "else", "api", "http", "header", "string", "object",
    "class", "edu", "fudan", "org", "springframework",
})

# Типы параметров без семантики (инфра/java stdlib)
_PARAM_STOPWORDS: frozenset[str] = frozenset({
    "HttpHeaders", "HttpServletRequest", "HttpServletResponse",
    "Principal", "String", "Long", "Integer", "Boolean",
    "Object", "List", "Map", "ResponseEntity", "HttpEntity",
})


def set_targets_root(root) -> None:
    """Установить корень исходников для ServiceMatcher (вызывать из predict._build_index)."""
    global _TARGETS_ROOT
    _TARGETS_ROOT = Path(root) if root is not None else None


def set_corpus(code_docs: dict, ui_docs: dict) -> None:
    """Сохранить BM25-корпус для обогащения BGE-M3 doc_texts.

    Вызывать из predict._build_index() после построения scorer.
    doc_texts обогащаются NLP-фильтрованными code+ui токенами:
      - body_tokens из исходников (flow_steps, logger_tags)
      - UI-контекст: Angular-токены (напр. "advance" для travel-plan-service)
      - params_as_written: типы DTO без инфра-шума
    Это позволяет BGE-M3 различать семантически близкие обработчики без хардкода.
    """
    global _CODE_DOCS, _UI_DOCS
    _CODE_DOCS = code_docs
    _UI_DOCS = ui_docs


def _init_service_matcher(ix) -> None:
    """P-93/P-106: лениво строит ServiceMatcher и SemanticReranker из кода скелета."""
    global _SERVICE_MATCHER, _RERANKER, _DOC_TEXTS
    if _SERVICE_MATCHER is not None:
        return
    modules = set(ix.module.values())
    _SERVICE_MATCHER = ServiceMatcher(modules, ix, root=_TARGETS_ROOT)

    # SemanticReranker: построить doc_texts и инициализировать BGE-M3
    _RERANKER = SemanticReranker()
    if _RERANKER.enabled:
        import sys as _sys
        from math import log as _log

        # TF-IDF: вычислить IDF токенов по всему corpus для фильтрации
        # Добавляем в doc_text только domain-специфичные токены (высокий IDF).
        # Общие токены ("account", "order", "trip") встречаются везде → низкий IDF → не добавляем.
        _all_nids = [nid for nid, _, _ in ix.handlers]
        _df: dict[str, int] = {}
        for _nid in _all_nids:
            _tok_set: set[str] = set()
            for _src in (_CODE_DOCS, _UI_DOCS):
                if _src is not None:
                    _tok_set.update(
                        t for t in _src.get(_nid, [])
                        if t not in _DOC_STOPWORDS and len(t) > 2
                    )
            for _tok in _tok_set:
                _df[_tok] = _df.get(_tok, 0) + 1
        _N = max(len(_all_nids), 1)
        # Порог: токен в ≤ 25% обработчиков → domain-специфичный
        _IDF_THRESHOLD = _log(_N / max(_N * 0.25, 1))

        def _is_informative(tok: str) -> bool:
            idf = _log(_N / max(_df.get(tok, 1), 1))
            return idf >= _IDF_THRESHOLD

        texts: dict[str, str] = {}
        for nid, verb, tpl in ix.handlers:
            node = ix.N.get(nid, {})
            name = node.get("name", "")
            owner_id = node.get("owner", "")
            owner_fqn = ix.N[owner_id]["fqn"] if owner_id in ix.N else ""
            class_simple = owner_fqn.split(".")[-1] if owner_fqn else ""
            module = ix.module.get(nid, "")
            path = "/" + "/".join(tpl) if tpl else ""

            # Структурная база: HTTP verb + URL + метод + класс + модуль
            base = f"{verb} {path} {name} {class_simple} {module}".strip()

            # Обогащение: только domain-специфичные токены из code_doc (TF-IDF фильтрация)
            # code_doc = flow_steps + logger_tags из исходников (enrichment).
            # UI-токены не используются: они содержат много общего Angular-кода
            # который не различает сервисы (boosting contacts для consign-запросов и т.п.).
            extra_parts: list[str] = []
            seen: set[str] = set(toks(base))
            if _CODE_DOCS is not None:
                for tok in _CODE_DOCS.get(nid, []):
                    if (tok not in _DOC_STOPWORDS and len(tok) > 2
                            and tok not in seen and _is_informative(tok)):
                        extra_parts.append(tok)
                        seen.add(tok)

            # Типы параметров: DTO-типы как семантический сигнал (без инфра-типов)
            for param in node.get("params_as_written", []):
                if param not in _PARAM_STOPWORDS:
                    for tok in toks(param):
                        if (tok not in _DOC_STOPWORDS and tok not in seen
                                and len(tok) > 2 and _is_informative(tok)):
                            extra_parts.append(tok)
                            seen.add(tok)

            texts[nid] = (base + " " + " ".join(extra_parts[:20])).strip()

        _DOC_TEXTS = texts
        print(
            f"[searcher] SemanticReranker готов, doc_texts={len(texts)} обработчиков",
            file=_sys.stderr,
        )


def _is_g_side(a: str, b: str, reason: str) -> bool:
    """Определить G-сторону в паре сиблингов.

    Для G-поездов: сервис без суффикса -other и без цифры в конце.
    """
    a_s = _strip(a)
    if reason.startswith("other_variant"):
        return not a_s.endswith("-other")
    if reason.startswith("numeric_variant"):
        return not (a_s and a_s[-1].isdigit())
    # trigram / прочие: более короткое имя = G-сторона (эвристика)
    return len(a_s) <= len(_strip(b))


_GOTHER_REASONS = frozenset({"other_variant", "numeric_variant"})

# Кеш func_variant пар: module → (sibling_module, is_basic_side)
# basic = без функционального суффикса (travel); plan = с суффиксом (travel-plan)
_FUNC_VARIANT_MAP: dict[str, tuple[str, bool]] | None = None

# step_types которые используют базовый (non-plan) сервис поиска
_BASIC_SEARCH_TYPES = frozenset({"search_train"})
# step_types которые используют план-сервис (advanced поиск)
_PLAN_SEARCH_TYPES = frozenset({"search_advanced"})


def _init_sibling_map(ix) -> None:
    """Вычислить кеш сиблингов из модулей index (один раз).

    G/OTHER пары (train type disambiguation):
    - numeric_variant: ts-travel-service / ts-travel2-service
    - other_variant: ts-order-service / ts-order-other-service
    - trigram_cosine: нестандартные пары

    Func-variant пары (basic/plan step_type disambiguation, P-75):
    - func_variant: ts-travel-service / ts-travel-plan-service
    - func_variant: ts-route-service / ts-route-plan-service
    """
    global _SIBLING_MAP, _FUNC_VARIANT_MAP
    if _SIBLING_MAP is not None:
        return

    modules = list(set(ix.module.values()))
    pairs = detect_all_siblings(modules)

    mapping: dict[str, tuple[str, bool]] = {}
    func_map: dict[str, tuple[str, bool]] = {}

    for a, b, _weight, reason in pairs:
        base_reason = reason.split("(")[0]

        if base_reason == "func_variant":
            # Func-variant: shorter stripped name = basic side (без суффикса)
            a_s, b_s = _strip(a), _strip(b)
            a_is_basic = len(a_s) <= len(b_s)
            basic_svc = a if a_is_basic else b
            plan_svc  = b if a_is_basic else a
            if basic_svc not in func_map and plan_svc not in func_map:
                func_map[basic_svc] = (plan_svc, True)   # True = basic сторона
                func_map[plan_svc]  = (basic_svc, False) # False = plan сторона
            continue

        # Фильтр: только G/OTHER пары для train_type disambiguation
        if base_reason not in _GOTHER_REASONS and not base_reason.startswith("trigram"):
            continue
        g_is_a = _is_g_side(a, b, reason)
        g_svc = a if g_is_a else b
        o_svc = b if g_is_a else a
        if g_svc not in mapping and o_svc not in mapping:
            mapping[g_svc] = (o_svc, True)
            mapping[o_svc] = (g_svc, False)

    _SIBLING_MAP = mapping
    _FUNC_VARIANT_MAP = func_map


def _sibling_of(module: str) -> str | None:
    if _SIBLING_MAP is None:
        return None
    entry = _SIBLING_MAP.get(module)
    return entry[0] if entry else None


def _is_g_service(module: str) -> bool:
    if _SIBLING_MAP is None:
        return False
    entry = _SIBLING_MAP.get(module)
    return entry is not None and entry[1]  # True = G-сторона


def _is_other_service(module: str) -> bool:
    if _SIBLING_MAP is None:
        return False
    entry = _SIBLING_MAP.get(module)
    return entry is not None and not entry[1]  # False = OTHER-сторона


# ---------------------------------------------------------------------------
# Disambiguation: убрать ненужный сиблинг из множества кандидатов
# ---------------------------------------------------------------------------

def _disambiguate(hs: set[str], train_type: str, ix) -> set[str]:
    """При наличии сиблинг-пар в hs — убрать ненужный по train_type.

    Если train_type неизвестен → возвращаем hs без изменений (conservative).
    """
    if not train_type:
        return hs

    to_remove: set[str] = set()
    for h in hs:
        mod = ix.module.get(h, "")
        sibling_mod = _sibling_of(mod)
        if sibling_mod is None:
            continue
        # Оба сиблинга в кандидатах
        sibling_h = {h2 for h2 in hs if ix.module.get(h2) == sibling_mod}
        if not sibling_h:
            continue

        if train_type == "G":
            # Оставить G-сервис, убрать OTHER-сервис
            if _is_other_service(mod):
                to_remove.add(h)
        elif train_type in ("OTHER", "D"):
            # Оставить OTHER-сервис, убрать G-сервис
            if _is_g_service(mod):
                to_remove.add(h)

    return hs - to_remove if to_remove else hs


def _func_sibling_of(module: str) -> str | None:
    """Вернуть func-variant сиблинг (plan ↔ basic), или None."""
    if _FUNC_VARIANT_MAP is None:
        return None
    entry = _FUNC_VARIANT_MAP.get(module)
    return entry[0] if entry else None


def _is_basic_func_service(module: str) -> bool:
    """True если сервис — базовая сторона func-variant пары (без plan-суффикса)."""
    if _FUNC_VARIANT_MAP is None:
        return False
    entry = _FUNC_VARIANT_MAP.get(module)
    return entry is not None and entry[1]


def _is_plan_func_service(module: str) -> bool:
    """True если сервис — plan-сторона func-variant пары (с функциональным суффиксом)."""
    if _FUNC_VARIANT_MAP is None:
        return False
    entry = _FUNC_VARIANT_MAP.get(module)
    return entry is not None and not entry[1]


def _disambiguate_func_variant(hs: set[str], step_type: str, ix) -> set[str]:
    """P-75: func-variant disambiguation по step_type (basic vs plan).

    search_train  → убрать plan-сторону (travel-plan, route-plan)
    search_advanced → убрать basic-сторону (travel, route)
    Оба присутствуют в hs → убираем не нужный.
    Только один → оставляем без изменений (консервативно).
    """
    if step_type not in _BASIC_SEARCH_TYPES and step_type not in _PLAN_SEARCH_TYPES:
        return hs

    to_remove: set[str] = set()
    for h in hs:
        mod = ix.module.get(h, "")
        sibling_mod = _func_sibling_of(mod)
        if sibling_mod is None:
            continue
        # Оба сиблинга в hs → disambiguation применима
        if not any(ix.module.get(h2) == sibling_mod for h2 in hs):
            continue

        if step_type in _BASIC_SEARCH_TYPES and _is_plan_func_service(mod):
            to_remove.add(h)
        elif step_type in _PLAN_SEARCH_TYPES and _is_basic_func_service(mod):
            to_remove.add(h)

    return hs - to_remove if to_remove else hs


# ---------------------------------------------------------------------------
# Основная функция (замена baseline ground())
# ---------------------------------------------------------------------------

def enhanced_ground(
    ix,
    scorer,
    files: dict,
    S: dict,
    beta: float = 0.0,
    delta: float = 0.0,
    tau: float = 0.05,
    admin_pen: float = 0.5,
    explain: bool = False,
    oracle: Oracle | None = None,
) -> set[str]:
    """Улучшенный entry point поиск.

    Аргументы совместимы с baseline_nlp.ground() для drop-in замены.
    """
    # Инициализировать кеши при первом вызове
    _init_sibling_map(ix)
    _init_service_matcher(ix)  # P-93/P-106: ServiceMatcher из кода скелета

    # Начальный admin-контекст: из title/description сценария
    _title_desc = S.get("title", "") + " " + S.get("description", "")
    is_admin = bool(ADMIN & set(toks(_title_desc)))
    entries, prev_files, touched = set(), set(), set()
    log = []

    for st in S.get("steps") or []:
        act = st.get("action") or ""

        # --- Шаг 1: базовые токены из action ---
        q = expand(query_toks(act))

        # --- Шаг 2: step_type + extra_tokens (Фаза 2.1) ---
        step_info = classify(act)

        # Обновить admin-контекст по типу шага (carry-over смены роли)
        if step_info.step_type == "admin_login":
            is_admin = True
        elif step_info.step_type == "login":
            # Явный вход пассажира сбрасывает admin-контекст
            is_admin = False

        # Inline admin override: шаг явно упоминает "administrator" (без carry-over)
        # Пример: "As the administrator, open the list of all orders"
        step_is_admin = is_admin or bool(ADMIN & set(toks(act)))

        if step_info.extra_tokens:
            # Добавляем токенизированные версии extra_tokens к запросу
            for extra in step_info.extra_tokens:
                q = q + toks(extra)

        # Убрать "contact" из запросов бронирования/оплаты/отмены:
        # в этих шагах "contact" = имя человека, не сервис.
        if step_info.step_type in ("book", "rebook", "pay", "cancel", "collect", "check_refund"):
            q = [t for t in q if t not in frozenset(["contact", "contacts"])]

        # Для cancel/check_refund: "collect" (из "collected order") тянет execute-service.
        # Убираем его — cancel/refund не про ticket collection.
        if step_info.step_type in ("cancel", "check_refund"):
            q = [t for t in q if t not in frozenset(["collect", "collected"])]

        # Для query_consign: "order" тянет SM в order-service (а нужен consign-service).
        if step_info.step_type == "query_consign":
            q = [t for t in q if t not in frozenset(["order", "orders"])]

        # --- P-93/P-106 online: domain augmentation через ServiceMatcher ---
        # BM25 по коду сервисов находит лучший кандидат → добавляет его
        # code-токены к запросу (аддитивно, не заменяет action vocab токены).
        if _SERVICE_MATCHER is not None:
            domain_extras = _SERVICE_MATCHER.augment(q)
            if domain_extras:
                q = q + domain_extras

        # --- Oracle carry-over: обновить oracle из текста шага (P-54) ---
        if oracle is not None:
            oracle.parse_step(act)

        # --- Шаг 3: ранжирование ---
        ranked = []
        for sc, h in scorer.rank(q):
            mod = ix.module.get(h, "")
            if not step_is_admin and (
                "admin" in mod
                or any("admin" in os.path.basename(f) for f in files.get(h, ()))
            ):
                sc *= admin_pen
            if files.get(h, set()) & prev_files:
                sc *= 1 + beta
            if mod in touched:
                sc *= 1 + delta
            ranked.append((sc, h))

        ranked.sort(reverse=True)

        # --- Hybrid scoring: BM25 + dense (BGE-M3) как основное ранжирование ---
        # alpha=0.5 → равный вес BM25 и dense.
        # Если модель недоступна — ranked остаётся BM25-only (прозрачный fallback).
        semantic_top3: list[tuple[float, str]] = []
        if _RERANKER is not None and _RERANKER.enabled and _DOC_TEXTS is not None:
            ranked = _RERANKER.rerank(act, ranked[:20], _DOC_TEXTS, alpha=0.5)
            semantic_top3 = ranked[:3]

        # --- Шаг 4: top + tau window ---
        hs: set[str] = set()
        if ranked:
            best = ranked[0][0]
            hs = {h for sc, h in ranked if sc >= best * (1 - tau)}

        # --- Шаг 5: disambiguation travel/order по train_type ---
        # carry-over: шаги из _STEP_TYPES_USE_CONTEXT наследуют train_type из oracle
        # если сам шаг не упоминает тип поезда явно (P-54)
        effective_train_type = step_info.train_type
        if (not effective_train_type
                and oracle is not None
                and oracle.train_type
                and step_info.step_type in _STEP_TYPES_USE_CONTEXT):
            effective_train_type = oracle.train_type
        hs = _disambiguate(hs, effective_train_type, ix)

        # P-75: func-variant disambiguation (basic ↔ plan) по step_type.
        # search_train → убрать plan-сторону (travel-plan-service и др.)
        # search_advanced → убрать basic-сторону (travel-service и др.)
        hs = _disambiguate_func_variant(hs, step_info.step_type, ix)

        # Func-variant force-swap: когда только неправильная сторона в hs
        # (правильная ниже tau-окна) — заменить на лучший обработчик правильного сиблинга.
        # search_train: travel-plan в окне, travel вне → swap на travel.
        # search_advanced: travel в окне, travel-plan вне → swap на travel-plan.
        if (step_info.step_type in _BASIC_SEARCH_TYPES
                or step_info.step_type in _PLAN_SEARCH_TYPES) and hs:
            swapped_fv: set[str] = set(hs)
            for h in list(hs):
                mod = ix.module.get(h, "")
                sibling_mod = _func_sibling_of(mod)
                if sibling_mod is None:
                    continue
                # Оба уже в hs — _disambiguate_func_variant уже справился
                if any(ix.module.get(h2) == sibling_mod for h2 in hs):
                    continue
                # Это неправильная сторона?
                wrong = (
                    (step_info.step_type in _BASIC_SEARCH_TYPES and _is_plan_func_service(mod))
                    or (step_info.step_type in _PLAN_SEARCH_TYPES and _is_basic_func_service(mod))
                )
                if not wrong:
                    continue
                # Найти лучший обработчик правильного сиблинга в ranked
                for _, h2 in ranked:
                    if ix.module.get(h2) == sibling_mod:
                        swapped_fv.discard(h)
                        swapped_fv.add(h2)
                        break
            hs = swapped_fv

        # Sibling force-swap: если train_type известен, но в hs только неправильный
        # сиблинг (второй вне tau-окна) — заменить на правильный из ranked.
        if effective_train_type and hs:
            swapped: set[str] = set(hs)
            for h in list(hs):
                mod = ix.module.get(h, "")
                sibling_mod = _sibling_of(mod)
                if sibling_mod is None:
                    continue
                # Оба уже в hs — disambiguation справился
                if any(ix.module.get(h2) == sibling_mod for h2 in hs):
                    continue
                # Проверить: это неправильная сторона?
                wrong = (
                    (effective_train_type in ("OTHER", "D") and _is_g_service(mod)) or
                    (effective_train_type == "G" and _is_other_service(mod))
                )
                if not wrong:
                    continue
                # Найти лучший обработчик правильного сиблинга в ranked
                for _, h2 in ranked:
                    if ix.module.get(h2) == sibling_mod:
                        swapped.discard(h)
                        swapped.add(h2)
                        break
            hs = swapped

        # Top-1 per module: один шаг сценария = один HTTP-вызов к каждому сервису
        # → оставить только top-1 handler per module в tau-окне.
        # Убирает дубликаты одного сервиса (напр. findContactsByAccountId + getContactsByContactsId).
        # ranked отсортирован по убыванию — первый встреченный по модулю и есть top-1.
        if hs:
            best_per_mod: dict[str, str] = {}
            for _, h in ranked:
                if h not in hs:
                    continue
                mod = ix.module.get(h, "")
                if mod not in best_per_mod:
                    best_per_mod[mod] = h
            hs = set(best_per_mod.values())

        entries |= hs
        prev_files = (
            set().union(*(files.get(h, set()) for h in hs))
            if hs
            else prev_files
        )
        if delta:
            touched |= {ix.module[m] for m in ix.closure(hs)[0]}

        log.append((st.get("n"), act, step_info, [(sc, h) for sc, h in ranked[:5]], hs, effective_train_type, semantic_top3))

    if explain:
        for n, act, si, top, hs, eff_tt, sem_top3 in log:
            print(
                f"  {n:2d}. {act[:70]}"
                f"  [type={si.step_type} train={eff_tt or '-'}]"
            )
            for sc, h in top:
                marker = "->" if h in hs else "  "
                mod = ix.module.get(h, "")
                name = ix.N[h].get("name", "")
                print(f"        {marker} {sc:.2f} {mod}.{name}")
            if sem_top3:
                bm25_h1 = top[0][1] if top else None
                sem_h1 = sem_top3[0][1] if sem_top3 else None
                flag = " [DIFF]" if bm25_h1 != sem_h1 else ""
                sem_strs = ", ".join(
                    f"{ix.module.get(h, '?')}.{ix.N[h].get('name', '?')}({sc:.2f})"
                    for sc, h in sem_top3
                )
                print(f"        [BGE-M3 top]{flag}: {sem_strs}")

    return entries
