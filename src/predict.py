#!/usr/bin/env python3
"""Предиктор execution trace для train-ticket.

Запуск:
    python src/predict.py scenarios/S0.json -o submission.json
    python src/predict.py scenarios/S0.json scenarios/S1.json -o submission.json

Архитектура (заменяемые слои):
    ground()       → src/localization/searcher.py   (Phase 2: свой BM25 + Token Taxonomy)
    ix.closure()   → src/trace/closure.py           (Phase 2: BFS с Oracle + Pruner)
    ix.submission  → src/output/submission.py       (Phase 3: precision control)
"""
import json
import os
import sys
import argparse
from pathlib import Path

_HERE    = Path(__file__).parent
_ROOT    = _HERE.parent
SKELETON = Path(os.getenv("TT_SKELETON", str(_ROOT / "tt-challenge" / "skeleton" / "trainticket.json.gz")))
TARGETS  = Path(os.getenv("TT_TARGETS",  str(_ROOT / "tt-challenge" / "targets" / "trainticket")))

sys.path.insert(0, str(_ROOT))
from src.core.skeleton_index import Index, load_json              # noqa: E402
from src.localization.ui_context import ui_context, handler_docs  # noqa: E402
from src.localization.scorer import Scorer                        # noqa: E402
from src.enrichment.signatures import build_extra_tokens          # noqa: E402
from src.localization.searcher import enhanced_ground, set_targets_root, set_corpus, init_code_encoders, flush_qwen_cache, batch_classify_steps  # noqa: E402
from src.similarity.cross_encoder import _save_ce_cache  # noqa: E402
from src.localization.flow_graph import FlowContext  # noqa: E402
from src.trace.closure import enhanced_closure, pruned_closure    # noqa: E402
from src.localization.service_classifier import (                 # noqa: E402
    detect_infra_remote,
    precompute_tier2_from_index,
)
from src.trace.oracle import Oracle                               # noqa: E402
from src.enrichment.ast_calls import patch_calls                  # noqa: E402


# ---------------------------------------------------------------------------
# Индекс (singleton: грузим один раз, используем для всех сценариев)
# ---------------------------------------------------------------------------

_index: Index | None = None
_scorer: Scorer | None = None
_files: dict | None = None
_project: str = ""


def _build_index(skeleton: Path, targets: Path, enrich: bool = True) -> None:
    global _index, _scorer, _files, _project, _INFRA_MODULES, _code_models, _code_gamma
    sk = load_json(str(skeleton))
    _index = Index(sk)
    _project = sk["project"]

    # P-93: авто-обнаружение Tier 2 из суффиксов классов скелета
    precompute_tier2_from_index(_index)
    # P-93/P-106: передать путь к исходникам для ServiceMatcher (@RequestMapping extraction)
    set_targets_root(targets)

    # Вычислить инфра-модули через Token Taxonomy (P-93)
    _INFRA_MODULES = detect_infra_remote(set(_index.module.values()))

    ui_tokens, _files, n = ui_context(str(targets), _index)
    print(f"UI: {n} мест вызова API, {len(_files)} обработчиков с контекстом", file=sys.stderr)

    code, ui = handler_docs(_index, ui_tokens)

    # Фаза 1: добавить logger_tags + flow_steps к code-документам
    if enrich:
        extras = build_extra_tokens(_index, targets)
        for nid, extra_toks in extras.items():
            if nid in code:
                code[nid] = code[nid] + extra_toks
            else:
                code[nid] = extra_toks

    _scorer = Scorer(code, ui)

    # P-93: передать BM25-корпус для обогащения BGE-M3 doc_texts:
    # ui-токены несут "advance" для travel-plan-service, "book" для preserve-service и т.д.
    set_corpus(code, ui)

    # AST patch: дописать рёбра calls из тел методов (конструкторы, enum-поля)
    n_patched = patch_calls(_index, targets)
    print(f"AST patch: +{n_patched} call-рёбер из исходников", file=sys.stderr)

    # Code encoders: инициализируем после set_corpus (нужен _DOC_TEXTS из _init_service_matcher)
    # Вызов здесь через CLI-флаг --code-model
    if _code_models:
        # Убедиться что _init_service_matcher уже запустился (лениво, через первый вызов)
        # Принудительно инициализируем ServiceMatcher сейчас
        from src.localization.searcher import _init_service_matcher as _ism
        _ism(_index)
        init_code_encoders(_code_models, _code_gamma)


# ---------------------------------------------------------------------------
# Precision-first: инфраструктурные сервисы не генерируют remote edges.
# Системные вызовы (auth, token, verifycode) происходят на каждый запрос,
# но в gold они отсутствуют — gold оценивает бизнес-логику.
#
# Инфра-сервисы определяются через Token Taxonomy (P-93), а не хардкодом.
# ---------------------------------------------------------------------------

# Вычисляется лениво после загрузки index (модули не известны заранее)
_INFRA_MODULES: frozenset[str] = frozenset()


def _module_of(node_id: str) -> str:
    parts = node_id.split(":", 2)
    return parts[1] if len(parts) >= 3 else ""


def _filter_remote(remote: set, no_remote: bool) -> set:
    """Убрать системные remote-рёбра (инфра-сервисы как source или target)."""
    if no_remote:
        return set()
    return {
        (src, tgt) for src, tgt in remote
        if _module_of(src) not in _INFRA_MODULES
        and _module_of(tgt) not in _INFRA_MODULES
    }


# ---------------------------------------------------------------------------
# Предсказание одного сценария
# ---------------------------------------------------------------------------

_use_enhanced: bool = True   # False → fallback к baseline ground()
_code_models: list[str] = []  # model names для code encoders
_code_gamma: float = 0.3      # вес code encoder в финальной оценке


def predict_scenario(
    S: dict,
    no_remote: bool = False,
    explain: bool = False,
    viz_scenario: dict | None = None,
) -> dict:
    """Принять сценарий S (dict из scenarios/*.json), вернуть блок edges для submission."""
    sid = S["id"]

    if _use_enhanced:
        # Oracle: создаётся на сценарий, накапливает train_type через carry-over (P-54)
        oracle = Oracle()

        # FlowContext: zero-hardcode граф-трекинг ветки сиблингов (параллельно oracle)
        flow_ctx = FlowContext(_index)

        step_log: list | None = [] if viz_scenario is not None else None

        # Pass 1: параллельная Qwen-классификация всех шагов до enhanced_ground.
        # Результаты кешируются в _STEP_CLASSIFY_CACHE (searcher.py) и используются
        # внутри enhanced_ground для override step_type + search_keywords.
        _step_actions = [st.get("action", "") for st in (S.get("steps") or [])]
        batch_classify_steps(_step_actions)

        # Шаг 1: найти entry points с oracle carry-over (P-54: disambiguation для шагов без явного train_type)
        entries = enhanced_ground(_index, _scorer, _files, S, oracle=oracle,
                                  flow_ctx=flow_ctx, explain=explain, viz_log=step_log)

        # Шаг 2: BFS-замыкание с Oracle Pruner (remote_call фильтрация по train_type)
        # oracle.train_type накоплен через parse_step в enhanced_ground
        train_type = oracle.train_type

        # Опциональные сервисы: исключить remote_call к сервисам которые в этом
        # сценарии не использовались (нет food/insurance в booking шагах).
        # Предотвращает FP remote-рёбра к ts-food-service / ts-assurance-service.
        _FOOD_MODS = frozenset({
            "ts-food-service", "ts-food-delivery-service",
            "ts-train-food-service", "ts-station-food-service",
        })
        _INS_MODS = frozenset({"ts-assurance-service"})
        _CONSIGN_MODS = frozenset({"ts-consign-service"})
        _CONSIGN_STEP_TYPES = {"add_consign", "update_consign", "query_consign"}
        excluded_modules: frozenset[str] = frozenset()
        if not oracle.has_food:
            excluded_modules = excluded_modules | _FOOD_MODS
        if not oracle.has_insurance:
            excluded_modules = excluded_modules | _INS_MODS
        # Consign: exclude если нет explicit luggage и нет consign-шагов в истории.
        # preserve-service BFS → createConsign только при actual luggage booking.
        _has_consign = oracle.has_luggage or any(
            sr.step_type in _CONSIGN_STEP_TYPES for sr in oracle.step_history
        )
        if not _has_consign:
            excluded_modules = excluded_modules | _CONSIGN_MODS

        # search_transit: entry-handler-ы этих шагов вызывают ОБА сиблинг-сервиса
        # (transfer search запрашивает все типы поездов), поэтому для них
        # sibling-фильтрацию отключаем — запускаем отдельный closure без train_type.
        # Остальные entries продолжают использовать стандартный train_type-фильтр.
        transit_entries: set[str] = set()
        for step_rec in oracle.step_history:
            if step_rec.step_type == "search_transit":
                transit_entries.update(step_rec.found_handlers)

        if transit_entries and train_type:
            t_methods, t_edges, t_remote, t_data = pruned_closure(
                transit_entries, _index, "", excluded_modules)
            other_entries = entries - transit_entries
            if other_entries:
                o_methods, o_edges, o_remote, o_data = pruned_closure(
                    other_entries, _index, train_type, excluded_modules)
            else:
                o_methods, o_edges, o_remote, o_data = set(), set(), set(), set()
            methods = t_methods | o_methods
            edges = t_edges | o_edges
            remote = t_remote | o_remote
            data = t_data | o_data
        else:
            methods, edges, remote, data = pruned_closure(
                entries, _index, train_type, excluded_modules)
    else:
        train_type = ""
        entries = enhanced_ground(_index, _scorer, _files, S)
        methods, edges, remote, data = enhanced_closure(entries, _index)
        step_log = None

    remote = _filter_remote(remote, no_remote)

    # Post-filter: zero-calls HTTP handlers = welcome/root endpoints (no business logic).
    # Топологически: @RequestMapping handler без вызовов → не участвует в trace.
    _zero_calls = {nid for nid, _, _ in _index.handlers if not _index.calls.get(nid)}
    methods = methods - _zero_calls
    edges = {(s, t) for s, t in edges if s not in _zero_calls and t not in _zero_calls}
    remote = {(s, t) for s, t in remote if t not in _zero_calls}

    print(
        f"{sid}: входов {len(entries)}, методов {len(methods)}, "
        f"calls {len(edges)}, remote {len(remote)}, data {len(data)}"
        f"  [train={train_type or '-'}]",
        file=sys.stderr,
    )

    if viz_scenario is not None and step_log is not None:
        viz_scenario[sid] = {
            "stats": {
                "entries": len(entries),
                "methods": len(methods),
                "calls": len(edges),
                "remote": len(remote),
                "data": len(data),
            },
            "steps": step_log,
        }

    return _index.submission(
        sid, methods, edges, remote, data,
        method="nlp+bm25+taxonomy+oracle",
    )


# ---------------------------------------------------------------------------
# Viz JSON builder для debug_viz.html
# ---------------------------------------------------------------------------

def _source_key(nid: str) -> str:
    """method:ts-auth:a.b.C#foo() → ts-auth.a.b.C#foo()"""
    parts = nid.split(":", 2)
    return f"{parts[1]}.{parts[2]}" if len(parts) == 3 else nid


def _nid_to_key(nid: str) -> str:
    """Универсальный ключ для любого nid (method или data_resource)."""
    if nid.startswith("method:"):
        return _source_key(nid)
    if nid.startswith("res:table:"):
        return nid[len("res:table:"):]
    return nid


def _write_viz(viz_explain: dict, viz_path: Path, targets: Path) -> None:
    """Собрать viz JSON и записать в файл."""
    source: dict = {}

    # Per-step BFS: populate trace.calls/remote/data
    for sid, scen in viz_explain.items():
        for step in scen.get("steps", []):
            selected_nids = set(step.pop("_selected_nids", []))
            train_type = step.pop("_train_type", "")
            if selected_nids:
                _, step_edges, step_remote, step_data = pruned_closure(
                    selected_nids, _index, train_type
                )
                step["trace"] = {
                    "calls": list({_nid_to_key(t) for _, t in step_edges}),
                    "remote": list({_nid_to_key(t) for _, t in step_remote}),
                    "data": list({_nid_to_key(t) for _, t in step_data}),
                }

    for sid, scen in viz_explain.items():
        for step in scen.get("steps", []):
            for cand in step.get("candidates", []):
                nid = cand.get("_nid")
                if not nid:
                    continue
                key = _source_key(nid)
                if key in source:
                    continue
                nd = _index.N.get(nid, {})
                file_rel = nd.get("file", "")
                line_s = nd.get("line") or 0
                line_e = nd.get("end") or line_s
                if not file_rel:
                    continue
                fpath = targets / file_rel
                try:
                    all_lines = fpath.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
                    src = "".join(all_lines[max(0, line_s - 1):line_e])
                except OSError:
                    src = ""
                source[key] = {"file": file_rel, "line": line_s, "end": line_e, "source": src}

    # Убрать внутренний _nid из кандидатов перед записью
    clean_explain: dict = {}
    for sid, scen in viz_explain.items():
        clean_steps = []
        for step in scen.get("steps", []):
            clean_cands = [{k: v for k, v in c.items() if k != "_nid"} for c in step["candidates"]]
            clean_steps.append({**step, "candidates": clean_cands})
        clean_explain[sid] = {**scen, "steps": clean_steps}

    # Граф сервисов из remote_call wiring
    svc_nodes: set[str] = set()
    svc_links: list[dict] = []
    seen_links: set[tuple] = set()
    for sid, scen in clean_explain.items():
        for step in scen.get("steps", []):
            for c in step["candidates"]:
                if c["selected"]:
                    svc_nodes.add(c["service"])
    for src_nid, tgts in _index.wiring.get("remote_call", {}).items():
        src_mod = _index.module.get(src_nid, "")
        for tgt_nid in tgts:
            tgt_mod = _index.module.get(tgt_nid, "")
            if src_mod and tgt_mod and src_mod != tgt_mod:
                edge = (src_mod, tgt_mod)
                if edge not in seen_links:
                    seen_links.add(edge)
                    svc_links.append({"source": src_mod, "target": tgt_mod})
                svc_nodes.add(src_mod)
                svc_nodes.add(tgt_mod)

    _sorted_svcs = sorted(svc_nodes)
    if len(_sorted_svcs) > 1:
        _pfx = os.path.commonprefix(_sorted_svcs)
        _sfx = os.path.commonprefix([s[::-1] for s in _sorted_svcs])[::-1]
    else:
        _pfx = _sfx = ""
    graph = {
        "nodes": [{"id": s, "label": s.removeprefix(_pfx).removesuffix(_sfx)} for s in _sorted_svcs],
        "links": svc_links,
    }

    viz = {"explain": clean_explain, "source": source, "scores": {}, "graph": graph}
    viz_path.write_text(json.dumps(viz, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"viz сохранён: {viz_path}", file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("scenarios", nargs="+", help="файлы сценариев scenarios/*.json")
    ap.add_argument("-o", "--out", required=True, help="выходной submission.json")
    ap.add_argument("--skeleton",    default=str(SKELETON), help="путь к skeleton.json.gz")
    ap.add_argument("--targets",     default=str(TARGETS),  help="корень исходников")
    ap.add_argument("--no-enrich",   action="store_true",   help="отключить Фазу 1 (enrichment)")
    ap.add_argument("--no-phase2",   action="store_true",   help="отключить Фазу 2 (enhanced_ground)")
    ap.add_argument("--no-remote",   action="store_true",   help="не предсказывать remote edges")
    ap.add_argument("--explain",     action="store_true",   help="вывести топ-5 кандидатов для каждого шага")
    ap.add_argument(
        "--code-model",
        action="append",
        dest="code_models",
        default=[],
        metavar="MODEL",
        help=(
            "добавить code encoder (можно несколько раз). "
            "Примеры: microsoft/codebert-base, microsoft/graphcodebert-base"
        ),
    )
    ap.add_argument(
        "--code-gamma",
        type=float,
        default=0.3,
        metavar="GAMMA",
        help="вес code encoder в финальной оценке (default: 0.3)",
    )
    ap.add_argument(
        "--viz",
        metavar="VIZ_FILE",
        help="сохранить debug JSON для debug_viz.html (например viz.json)",
    )
    a = ap.parse_args()

    global _use_enhanced, _code_models, _code_gamma
    _use_enhanced = not a.no_phase2
    _code_models = a.code_models
    _code_gamma = a.code_gamma

    _build_index(Path(a.skeleton), Path(a.targets), enrich=not a.no_enrich)

    viz_explain: dict | None = {} if a.viz else None

    out: dict = {"project": _project, "scenarios": {}}
    for path in a.scenarios:
        S = load_json(path)
        sid = S["id"]
        if a.explain:
            print(f"=== {sid} ===")
        out["scenarios"][sid] = predict_scenario(
            S,
            no_remote=a.no_remote,
            explain=a.explain,
            viz_scenario=viz_explain,
        )

    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print(f"сохранено: {a.out}", file=sys.stderr)

    flush_qwen_cache()
    _save_ce_cache()

    if a.viz and viz_explain:
        _write_viz(viz_explain, Path(a.viz), Path(a.targets))


if __name__ == "__main__":
    main()
