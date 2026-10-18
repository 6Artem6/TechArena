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
from src.similarity.code_encoder import CodeEncoder  # noqa: E402
from src.similarity.cross_encoder import CrossEncoder  # noqa: E402
from src.localization.hub_scorer import precompute_reachability, rerank_by_hub  # noqa: E402
from src.localization.session_context import SessionContext  # noqa: E402
from src.localization.java_context import build_java_context, build_nl_enrichment  # noqa: E402
from src.localization.qwen_encoder import QwenEncoder, get_candidate_handlers_indexed  # noqa: E402


# Типы шагов, которые наследуют train_type из предыдущего шага через oracle carry-over.
# "search_train" устанавливает train_type, эти шаги используют его если не переопределили сами.
_STEP_TYPES_USE_CONTEXT = frozenset({
    "book", "pay", "collect", "cancel", "rebook",
    "view_trip", "query_food_trip",
    "add_consign", "query_consign", "change_seat",
})

# Типы шагов для которых Pass 1 search_keywords безопасны.
# Для search/search_advanced/view_trip keywords вроде "cheapest"/"trips" сдвигают BM25 вредно.
_SAFE_KEYWORD_TYPES = frozenset({
    "book", "pay", "cancel", "collect", "rebook",
    "enter_station", "add_consign", "query_consign",
    "check_price", "check_refund",
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

# SemanticReranker — BGE-M3 bi-encoder (этап 1 гибридного пайплайна)
_RERANKER: "SemanticReranker | None" = None
# CrossEncoder — bge-reranker-base (этап 2: joint cross-attention, top-20)
_CROSS_ENCODER: "CrossEncoder | None" = None
# Текстовые описания обработчиков для dense/cross кодирования {handler_id → text}
_DOC_TEXTS: "dict[str, str] | None" = None
# CE-специфичный doc_text: verb+path+name + javadoc + logger msgs + соседи класса
_CE_DOC_TEXTS: "dict[str, str] | None" = None

# Code encoders: CodeBERT / GraphCodeBERT (дополнительный сигнал поверх BM25+BGE-M3)
_CODE_ENCODERS: "list[tuple[CodeEncoder, float]]" = []  # [(encoder, gamma), ...]

# Qwen2.5-Coder query decomposer (ollama, локальный)
_QWEN_ENCODER: "QwenEncoder | None" = None

# Корпус BM25 (code + ui) — передаётся из predict._build_index() для обогащения doc_texts
_CODE_DOCS: "dict[str, list[str]] | None" = None
_UI_DOCS:   "dict[str, list[str]] | None" = None

# _internal_type_names: простые имена типов из скелета проекта.
# Вычисляется лениво при первой инициализации SemanticReranker.
# Заменяет _PARAM_STOPWORDS и _DOC_STOPWORDS — граф сам знает что внутреннее.
_internal_type_names: frozenset[str] = frozenset()


def _build_internal_type_names(ix) -> frozenset[str]:
    """Простые имена всех type-узлов проекта из скелета (без дженериков)."""
    result: set[str] = set()
    for v in ix.N.values():
        if v.get("kind") == "type" and v.get("fqn"):
            result.add(v["fqn"].split(".")[-1])
    return frozenset(result)


def flush_qwen_cache() -> None:
    """Сохранить Qwen-кеш на диск (вызывается в конце predict.py)."""
    if _QWEN_ENCODER is not None:
        _QWEN_ENCODER.flush()


# Pass 1: кеш результатов Qwen-классификации шагов {action → QwenClassifyOutput}
_STEP_CLASSIFY_CACHE: dict[str, object] = {}


def batch_classify_steps(actions: list[str]) -> list:
    """Pass 1: параллельная классификация шагов сценария через Qwen.

    Возвращает list[QwenClassifyOutput] в том же порядке что и actions.
    Пустой список если Qwen недоступен.
    Инициализирует QwenEncoder если ещё не запущен (он может быть вызван
    до enhanced_ground, поэтому не ждём _init_service_matcher).
    """
    global _STEP_CLASSIFY_CACHE, _QWEN_ENCODER
    if _QWEN_ENCODER is None:
        _QWEN_ENCODER = QwenEncoder(targets_root=_TARGETS_ROOT)
    if not _QWEN_ENCODER.enabled:
        return []
    results = _QWEN_ENCODER.batch_classify_steps(actions)
    _STEP_CLASSIFY_CACHE = {action: r for action, r in zip(actions, results)}
    return results


def get_step_classify(action: str):
    """Получить результат Pass 1 для конкретного шага (None если нет)."""
    return _STEP_CLASSIFY_CACHE.get(action)


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


def init_code_encoders(model_names: list[str], gamma: float = 0.3) -> None:
    """Инициализировать code encoders (CodeBERT/GraphCodeBERT).

    Вызывать из predict._build_index() после set_corpus(), чтобы _DOC_TEXTS был готов.
    model_names: список model_name-ов HuggingFace для загрузки.
    gamma: вес code encoder в финальной оценке (0 = без влияния, 0.3 = 30%).
    """
    global _CODE_ENCODERS
    if not model_names:
        return
    # Убедиться, что _DOC_TEXTS построены (нужен _init_service_matcher)
    # code encoders инициализируются ПОСЛЕ _init_service_matcher
    _CODE_ENCODERS = []
    for mname in model_names:
        enc = CodeEncoder(mname)
        if enc.enabled and _DOC_TEXTS:
            enc.precompute(_DOC_TEXTS)
            _CODE_ENCODERS.append((enc, gamma))
            import sys as _sys
            print(
                f"[searcher] CodeEncoder [{mname.split('/')[-1]}] готов, gamma={gamma}",
                file=_sys.stderr,
            )
        else:
            import sys as _sys
            print(
                f"[searcher] CodeEncoder [{mname}] недоступен или нет doc_texts",
                file=_sys.stderr,
            )


def _init_service_matcher(ix) -> None:
    """P-93/P-106: лениво строит ServiceMatcher и SemanticReranker из кода скелета."""
    global _SERVICE_MATCHER, _RERANKER, _CROSS_ENCODER, _DOC_TEXTS, _CE_DOC_TEXTS, _internal_type_names
    if _SERVICE_MATCHER is not None:
        return
    modules = set(ix.module.values())
    _SERVICE_MATCHER = ServiceMatcher(modules, ix, root=_TARGETS_ROOT)

    # Внутренние типы из скелета — заменяет ручные _PARAM_STOPWORDS и _DOC_STOPWORDS
    _internal_type_names = _build_internal_type_names(ix)

    # Инициализировать Qwen ПЕРВЫМ — нужен для passport generation до BGE-M3
    global _QWEN_ENCODER
    if _QWEN_ENCODER is None:
        _QWEN_ENCODER = QwenEncoder(targets_root=_TARGETS_ROOT)

    # Загрузить паспорта сервисов (офлайн, кешируются в ~/.cache/service_passports.json).
    # Паспорта содержат business_summary + synonyms для каждого handler-а, генерируются
    # Qwen из исходного кода контроллеров (не хардкод).
    from src.localization.passport_generator import load_or_generate_passports
    _passports = load_or_generate_passports(ix, _TARGETS_ROOT, _QWEN_ENCODER)

    # SemanticReranker: построить doc_texts и инициализировать BGE-M3
    _RERANKER = SemanticReranker()
    if _RERANKER.enabled:
        import sys as _sys
        from math import log as _log

        # TF-IDF: вычислить IDF токенов по всему corpus для фильтрации.
        # High-frequency токены ("get", "set", "edu", "null") имеют высокий df → IDF≈0 →
        # автоматически отфильтровываются _is_informative без ручного списка стоп-слов.
        _all_nids = [nid for nid, _, _ in ix.handlers]
        _df: dict[str, int] = {}
        for _nid in _all_nids:
            _tok_set: set[str] = set()
            for _src in (_CODE_DOCS, _UI_DOCS):
                if _src is not None:
                    _tok_set.update(
                        t for t in _src.get(_nid, []) if len(t) > 2
                    )
            for _tok in _tok_set:
                _df[_tok] = _df.get(_tok, 0) + 1
        _N = max(len(_all_nids), 1)
        # Порог: токен в ≤ 25% обработчиков → domain-специфичный
        _IDF_THRESHOLD = _log(_N / max(_N * 0.25, 1))

        def _is_informative(tok: str) -> bool:
            idf = _log(_N / max(_df.get(tok, 1), 1))
            return idf >= _IDF_THRESHOLD

        # NL-обогащение из Java source (de-bracketed logger content + flow steps).
        # Вычисляется до основного цикла; в цикле фильтруется через _is_informative.
        _nl_enrich = build_nl_enrichment(ix, _TARGETS_ROOT)

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
            extra_parts: list[str] = []
            seen: set[str] = set(toks(base))
            if _CODE_DOCS is not None:
                for tok in _CODE_DOCS.get(nid, []):
                    if len(tok) > 2 and tok not in seen and _is_informative(tok):
                        extra_parts.append(tok)
                        seen.add(tok)

            # Типы параметров: только internal domain-типы из скелета (не инфра)
            for param in node.get("params_as_written", []):
                simple = param.split(".")[-1].split("<")[0].strip()
                if simple in _internal_type_names:
                    for tok in toks(param):
                        if len(tok) > 2 and tok not in seen and _is_informative(tok):
                            extra_parts.append(tok)
                            seen.add(tok)

            # Java NL semantic enrichment: de-bracketed logger content + flow step words.
            # Добавляет domain NL vocabulary (напр. "transit", "inbound", "detail") в BGE-M3 doc.
            for nl_tok in _nl_enrich.get(nid, []):
                if len(nl_tok) > 2 and nl_tok not in seen and _is_informative(nl_tok):
                    extra_parts.append(nl_tok)
                    seen.add(nl_tok)

            # Passport synonyms: Qwen-генерированные бизнес-термины из кода контроллера.
            # Не фильтруются через _is_informative — они уже специфичны для данного endpoint.
            _p = _passports.get(nid)
            if _p:
                for _syn in _p.synonyms[:8]:
                    for _tok in toks(_syn):
                        if len(_tok) > 2 and _tok not in seen:
                            extra_parts.append(_tok)
                            seen.add(_tok)

            texts[nid] = (base + " " + " ".join(extra_parts[:40])).strip()

        _DOC_TEXTS = texts
        print(
            f"[searcher] SemanticReranker готов, doc_texts={len(texts)} обработчиков",
            file=_sys.stderr,
        )

    # CrossEncoder: bge-reranker-base — joint cross-attention поверх BGE-M3 кандидатов
    _CROSS_ENCODER = CrossEncoder()
    if _CROSS_ENCODER.enabled:
        import sys as _sys2
        print("[searcher] CrossEncoder bge-reranker-base готов", file=_sys2.stderr)

    # CE doc_texts: иерархический контекст из Java исходников (сервис → класс → метод).
    # Включает javadoc, logger messages, flow_steps, соседей класса.
    # Объединяется с _DOC_TEXTS (verb+path+name) для CE — богаче BM25 doc_texts.
    # + паспортные synonyms и business_summary для различения близких handler-ов.
    java_ctx = build_java_context(ix, _TARGETS_ROOT)
    base_texts = _DOC_TEXTS or {}
    _CE_DOC_TEXTS = {}
    for nid in set(list(base_texts) + list(java_ctx)):
        parts = [base_texts.get(nid, ""), java_ctx.get(nid, "")]
        _p = _passports.get(nid)
        if _p:
            parts.append(_p.business_summary)
            parts.append(" ".join(_p.synonyms))
        _CE_DOC_TEXTS[nid] = " ".join(p for p in parts if p).strip()
    import sys as _sys3
    print(
        f"[searcher] CE doc_texts: {len(java_ctx)} handlers enriched "
        f"(Java context + {sum(1 for n in java_ctx if n in _passports)} passports)",
        file=_sys3.stderr,
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


def _disambiguate_func_variant(hs: set[str], step_type: str, ix, ranked: list | None = None) -> set[str]:
    """P-75: func-variant disambiguation по step_type (basic vs plan).

    search_train  → убрать plan-сторону (travel-plan, route-plan); force-swap если только plan в hs
    search_advanced → убрать basic-сторону; force-swap если только basic в hs
    """
    if step_type not in _BASIC_SEARCH_TYPES and step_type not in _PLAN_SEARCH_TYPES:
        return hs

    want_basic = step_type in _BASIC_SEARCH_TYPES  # True = хотим basic, убрать plan

    result = set(hs)
    for h in hs:
        mod = ix.module.get(h, "")
        sibling_mod = _func_sibling_of(mod)
        if sibling_mod is None:
            continue
        is_plan = _is_plan_func_service(mod)
        is_basic = _is_basic_func_service(mod)

        if want_basic and is_plan:
            # Хотим basic, но в hs — plan side
            sibling_in_hs = any(ix.module.get(h2) == sibling_mod for h2 in hs)
            if sibling_in_hs:
                # Оба в hs → просто убрать plan
                result.discard(h)
            elif ranked is not None:
                # Только plan в hs → force-swap: найти basic-side handler в ranked
                for _, h2 in ranked:
                    if ix.module.get(h2) == sibling_mod:
                        result.discard(h)
                        result.add(h2)
                        break
        elif not want_basic and is_basic:
            # Хотим plan, но в hs — basic side
            sibling_in_hs = any(ix.module.get(h2) == sibling_mod for h2 in hs)
            if sibling_in_hs:
                result.discard(h)
            elif ranked is not None:
                for _, h2 in ranked:
                    if ix.module.get(h2) == sibling_mod:
                        result.discard(h)
                        result.add(h2)
                        break

    return result


# Таблица: step_type → глагол-ключ в имени метода.
# Для cancel/rebook/register: предпочитаем handler чьё имя содержит этот глагол.
_STEP_TYPE_VERB: dict[str, str] = {
    "cancel": "cancel",
    "rebook": "rebook",
    "register": "register",
}


def _prefer_step_typed_handler(
    hs: set[str], step_type: str, ix, ranked: list
) -> set[str]:
    """Предпочесть handler с именем, содержащим глагол шага.

    Для cancel/rebook: если в tau-window несколько handler'ов одного модуля
    и один из них содержит verb в имени метода → оставить только его.
    Если typed handler вне tau-window → принудительно добавить из ranked
    (force-swap), убрав нетипизированный.

    Примеры:
      cancel step → cancelTicket (vs calculate) — cancelTicket имеет "cancel"
      rebook step → rebook.rebook (vs payDifference) — rebook.rebook имеет "rebook"
    """
    verb = _STEP_TYPE_VERB.get(step_type, "")
    if not verb or not hs:
        return hs

    by_mod: dict[str, list[str]] = {}
    for h in hs:
        by_mod.setdefault(ix.module.get(h, ""), []).append(h)

    result = set(hs)
    for mod, handlers in by_mod.items():
        typed   = [h for h in handlers if verb in ix.N.get(h, {}).get("name", "").lower()]
        untyped = [h for h in handlers if verb not in ix.N.get(h, {}).get("name", "").lower()]

        if typed:
            # Typed handler уже в окне → убрать нетипизированных из того же модуля
            for h in untyped:
                result.discard(h)
        elif untyped:
            # Typed handler вне tau-window → force-swap из ranked (сначала тот же модуль, затем любой)
            found = False
            for _, h2 in ranked:
                if (ix.module.get(h2) == mod
                        and verb in ix.N.get(h2, {}).get("name", "").lower()):
                    for h in untyped: result.discard(h)
                    result.add(h2)
                    found = True
                    break
            if not found:
                # Если verb нет в текущем модуле → ищем в любом модуле ranked (cross-module swap)
                for _, h2 in ranked:
                    if verb in ix.N.get(h2, {}).get("name", "").lower():
                        for h in untyped: result.discard(h)
                        result.add(h2)
                        break
    return result


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
    flow_ctx=None,
    viz_log: list | None = None,
) -> set[str]:
    """Улучшенный entry point поиск.

    Аргументы совместимы с baseline_nlp.ground() для drop-in замены.
    """
    # Инициализировать кеши при первом вызове
    _init_sibling_map(ix)
    _init_service_matcher(ix)  # P-93/P-106: ServiceMatcher из кода скелета
    _hub_reachable = precompute_reachability(ix)

    # Начальный admin-контекст: из title/description сценария
    _title_desc = S.get("title", "") + " " + S.get("description", "")
    is_admin = bool(ADMIN & set(toks(_title_desc)))
    entries, prev_files, touched = set(), set(), set()
    log = []

    # Сессионный контекст: NLP-обогащение BM25-запросов на основе истории шагов
    session_ctx = SessionContext()

    for step_idx, st in enumerate(S.get("steps") or []):
        act = st.get("action") or ""

        # --- Шаг 1: базовые токены из action ---
        q = expand(query_toks(act))

        # --- Шаг 2: step_type + extra_tokens (Фаза 2.1) ---
        step_info = classify(act)

        # Pass 1: применять search_keywords только для шагов где они безопасны.
        # Для search/search_advanced/view_trip keywords "cheapest"/"trips" сдвигают BM25 вредно.
        # Для book/pay/cancel/collect keywords "inside"/"first class" безопасны и помогают.
        _qwen_classify = get_step_classify(act)
        if (_qwen_classify is not None and _qwen_classify.search_keywords
                and step_info.step_type in _SAFE_KEYWORD_TYPES):
            step_info.extra_tokens = list(set(
                step_info.extra_tokens + _qwen_classify.search_keywords
            ))

        # Обновить admin-контекст по типу шага (carry-over смены роли)
        if step_info.step_type == "admin_login":
            is_admin = True
        elif step_info.step_type == "login":
            # Явный вход пассажира сбрасывает admin-контекст
            is_admin = False

        # Inline admin override: шаг явно упоминает "administrator" (без carry-over)
        # Пример: "As the administrator, open the list of all orders"
        step_is_admin = is_admin or bool(ADMIN & set(toks(act)))

        # --- Сессионный контекст: обогащение BM25-запроса историей шагов ---
        ctx_tokens = session_ctx.context_tokens(step_info.step_type, step_info, act)
        session_ctx.update(step_info.step_type, step_info, act)

        # act_extended: action + extra_tokens — передаётся в BGE-M3 reranker
        # чтобы semantic scoring тоже учитывал structural disambiguation tokens.
        act_extended = act
        all_extra = step_info.extra_tokens + ctx_tokens
        if all_extra:
            for extra in all_extra:
                q = q + toks(extra)
            act_extended = act + " " + " ".join(all_extra)

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

        # Для external pay: "internal balance is empty" → убрать "internal"/"inside"/"balance"
        # чтобы не буcтить ts-inside-payment-service; нужен ts-payment-service
        if step_info.step_type == "pay" and any(t in ("payment", "service") for t in step_info.extra_tokens):
            q = [t for t in q if t not in frozenset(["internal", "inside", "balance"])]

        # --- P-93/P-106 online: domain augmentation через ServiceMatcher ---
        # BM25 по коду сервисов находит лучший кандидат → добавляет его
        # code-токены к запросу (аддитивно, не заменяет action vocab токены).
        # Для поисковых шагов ServiceMatcher шумит (выбирает cancel вместо travel) → пропускаем.
        # Для поисковых шагов ServiceMatcher шумит или выбирает не тот сервис → пропускаем.
        _SEARCH_STEP_TYPES = frozenset({"search_train", "search_advanced", "search_transit"})
        if _SERVICE_MATCHER is not None and step_info.step_type not in _SEARCH_STEP_TYPES:
            domain_extras = _SERVICE_MATCHER.augment(q)
            if domain_extras:
                q = q + domain_extras

        # --- Qwen index-based handler selection ---
        # Стратегия: Qwen получает пронумерованный список кандидатов →
        #   возвращает {index, confidence} →
        #   прямой inject nid из nid_map без graph_name_lookup.
        # Инжект только при index > 0 и confidence >= MIN_CONFIDENCE.
        from src.localization.qwen_encoder import _MIN_CONFIDENCE  # noqa: PLC0415
        _bge_query = act_extended
        _qwen_result = None
        _qwen_injected_nid: str | None = None
        _cand_nids: dict[int, str] = {}
        if _QWEN_ENCODER is not None and _QWEN_ENCODER.enabled and oracle is not None:
            _cand_display, _cand_nids = get_candidate_handlers_indexed(oracle, ix)
            _qwen_result = _QWEN_ENCODER.decompose(act, oracle.step_history, _cand_display)
            if _qwen_result.index > 0 and _qwen_result.confidence >= _MIN_CONFIDENCE:
                _qwen_injected_nid = _cand_nids.get(_qwen_result.index)
        # BGE-M3 query: не используем Qwen query — слишком специфичен, вызывает регрессию.
        # _bge_query остаётся act_extended всегда.

        # --- Oracle carry-over: обновить oracle из текста шага (P-54) ---
        if oracle is not None:
            oracle.parse_step(act)
            oracle.update_from_step_type(step_info.step_type)

        # External pay: нужен и inside-payment И external payment service в entry points
        _is_external_pay = (
            step_info.step_type == "pay"
            and any(t in ("payment", "service") for t in step_info.extra_tokens)
            and not any(t == "inside" for t in step_info.extra_tokens)
        )

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
            ranked = _RERANKER.rerank(_bge_query, ranked[:20], _DOC_TEXTS, alpha=0.5)
            semantic_top3 = ranked[:3]

        # --- Code encoder reranking: CodeBERT/GraphCodeBERT поверх BM25+BGE-M3 ---
        for _ce, _gamma in _CODE_ENCODERS:
            ranked = _ce.rerank(act, ranked, gamma=_gamma)

        # --- Cross-encoder reranking: bge-reranker-base joint cross-attention ---
        # Этап 3 пайплайна: видит (query, doc) вместе → лучше различает
        # "cancel" ↔ "cancelTicket" vs "calculatePrice", "rebook" vs "book".
        # _CE_DOC_TEXTS: verb+path+name + javadoc + logger msgs + class neighbors.
        _ce_docs = _CE_DOC_TEXTS if _CE_DOC_TEXTS is not None else _DOC_TEXTS
        if _CROSS_ENCODER is not None and _CROSS_ENCODER.enabled and _ce_docs is not None:
            ranked = _CROSS_ENCODER.rerank(act, ranked, _ce_docs, top_k=20, alpha=0.3)

        # --- Graph flow boost (zero-hardcode: topology-based branch consistency) ---
        if flow_ctx is not None:
            ranked = [(sc + flow_ctx.boost(h, ix), h) for sc, h in ranked]
            ranked.sort(reverse=True)

        # view_trip с известным oracle.train_type: расширить tau-окно, чтобы travel service
        # попал в hs несмотря на высокий score preserve-service (vocabulary перекрытие в BGE-M3).
        # preserve-service доминирует ~0.85, travel2 ~0.80: стандартный tau=0.05 → порог 0.8075
        # (travel2 вылетает). tau=0.08 → порог 0.782 → travel2 входит.
        _step_tau = tau
        if step_info.step_type == "view_trip" and oracle is not None and oracle.train_type:
            _step_tau = min(tau + 0.03, 0.10)

        # --- Шаг 4: top + tau window ---
        hs: set[str] = set()
        if ranked:
            best = ranked[0][0]
            hs = {h for sc, h in ranked if sc >= best * (1 - _step_tau)}

        # --- Continuity: active service handlers с расширенным tau-окном ---
        # Oracle.active_services накапливается по шагам: book → pay → cancel/collect
        # используют те же order-сервисы. Handlers из active services получают
        # расширенный порог best*(1-tau*2), чтобы не потерять continuity-сервис
        # если он чуть ниже стандартного tau-окна.
        # Важно: continuity использует базовый tau (не _step_tau), чтобы tau-widening
        # для view_trip не ослаблял continuity-фильтр.
        if oracle is not None and ranked:
            best = ranked[0][0]
            continuity_threshold = best * (1.0 - tau * 2)
            for sc, h in ranked:
                if sc < continuity_threshold:
                    break
                if h in hs:
                    continue
                h_svc = ix.module.get(h, "")
                if h_svc and oracle.is_service_active(h_svc):
                    hs.add(h)

        # --- enter_station: force-add execute-service handler ---
        # "board the train" / "enter the station" — vocabulary не пересекается с
        # кодом ("ticketExecute", "executeOrder"). step_type == "enter_station" →
        # гарантированно нужен execute-service; берём лучший handler из ranked.
        if step_info.step_type == "enter_station" and ranked:
            for _sc, _h in ranked:
                _h_svc = ix.module.get(_h, "")
                if _h_svc and "execute" in _h_svc:
                    hs.add(_h)
                    break

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
        hs = _disambiguate_func_variant(hs, step_info.step_type, ix, ranked)

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

        # cancel/rebook: предпочесть handler с именем-глаголом шага
        # (cancelTicket vs calculate, rebook.rebook vs rebook.payDifference).
        # Вызывается до top-1 per module: меняет состав hs, не только порядок.
        hs = _prefer_step_typed_handler(hs, step_info.step_type, ix, ranked)

        # External pay: добавить best handler из external payment service в entry points.
        # Оба и inside-payment И external payment участвуют в execution trace
        # (inside вызывается всегда; external добавляется при external channel).
        if _is_external_pay:
            _ext_mods = {ix.module.get(h, "") for h in hs}
            for _, h2 in ranked:
                mod2 = ix.module.get(h2, "")
                # Искать payment service без "inside" который ещё не в hs
                if "payment" in mod2 and "inside" not in mod2 and mod2 not in _ext_mods:
                    hs.add(h2)
                    break

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

        # --- Qwen index injection: заменить top-1 в модуле на Qwen-выбранный handler ---
        # Условие: Qwen вернул валидный index + confidence >= MIN_CONFIDENCE +
        #          lexical filter: хотя бы одно слово ≥6 букв из имени handler-а
        #          присутствует в тексте action (предотвращает галлюцинации).
        if _qwen_injected_nid and hs:
            _gmod = ix.module.get(_qwen_injected_nid, "")
            if _gmod in best_per_mod and best_per_mod[_gmod] != _qwen_injected_nid:
                # Lexical filter: имя нового handler-а должно семантически
                # пересекаться с текстом шага (хотя бы одно слово ≥6 букв).
                _inj_name = ix.N.get(_qwen_injected_nid, {}).get("name", "")
                _inj_words = [w for w in toks(_inj_name) if len(w) >= 6]
                _act_lower = act.lower()
                if _inj_words and any(w in _act_lower for w in _inj_words):
                    _prev_name = ix.N.get(best_per_mod[_gmod], {}).get("name", "?")
                    best_per_mod[_gmod] = _qwen_injected_nid
                    hs = set(best_per_mod.values())
                    print(
                        f"[qwen] {_gmod.replace('ts-','').replace('-service','')} "
                        f"{_prev_name} → {_inj_name} "
                        f"(conf={_qwen_result.confidence:.2f})",
                        file=sys.stderr,
                    )

        # Записать граф-состояние шага для следующего (zero-hardcode flow tracking)
        if flow_ctx is not None and hs:
            flow_ctx.record(hs, ix)

        # Запомнить активные сервисы для continuity на следующих шагах
        if oracle is not None:
            for h in hs:
                svc = ix.module.get(h, "")
                if svc:
                    oracle.mark_service_active(svc)
            # Сформировать handler_names для Qwen-истории: "short-mod.methodName"
            _h_names = []
            for h in hs:
                mod = ix.module.get(h, "")
                name = ix.N.get(h, {}).get("name", "")
                if mod and name:
                    short = mod.replace("ts-", "").replace("-service", "")
                    _h_names.append(f"{short}.{name}")
            # Graph-guided session history: remote_call-соседи текущего шага
            oracle.record_step(
                step_idx, step_info.step_type, hs,
                ix.wiring, ix.module, ix.calls,
                handler_names=_h_names,
            )

        entries |= hs
        prev_files = (
            set().union(*(files.get(h, set()) for h in hs))
            if hs
            else prev_files
        )
        if delta:
            touched |= {ix.module[m] for m in ix.closure(hs)[0]}

        log.append((st.get("n"), act, step_info, [(sc, h) for sc, h in ranked[:5]], hs, effective_train_type, semantic_top3))

        if viz_log is not None:
            viz_log.append({
                "num": st.get("n", 0),
                "text": act,
                "type": step_info.step_type,
                "train": effective_train_type or "-",
                "candidates": [
                    {
                        "service": ix.module.get(h, ""),
                        "method": h.split(":", 2)[2] if h.count(":") >= 2 else h,
                        "score": round(float(sc), 4),
                        "selected": h in hs,
                        "_nid": h,
                    }
                    for sc, h in ranked[:5]
                ],
                "_selected_nids": list(hs),
                "_train_type": effective_train_type or "",
                "trace": {},
            })

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
