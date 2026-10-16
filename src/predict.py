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
import sys
import argparse
from pathlib import Path

_HERE    = Path(__file__).parent
_ROOT    = _HERE.parent
SKELETON = _ROOT / "tt-challenge" / "skeleton" / "trainticket.json.gz"
TARGETS  = _ROOT / "tt-challenge" / "targets" / "trainticket"

sys.path.insert(0, str(_ROOT))
from src.core.skeleton_index import Index, load_json              # noqa: E402
from src.localization.ui_context import ui_context, handler_docs  # noqa: E402
from src.localization.scorer import Scorer                        # noqa: E402
from src.enrichment.signatures import build_extra_tokens          # noqa: E402
from src.localization.searcher import enhanced_ground, set_targets_root, set_corpus  # noqa: E402
from src.trace.closure import enhanced_closure, pruned_closure    # noqa: E402
from src.localization.service_classifier import (                 # noqa: E402
    detect_infra_remote,
    precompute_tier2_from_index,
)
from src.trace.oracle import Oracle                               # noqa: E402


# ---------------------------------------------------------------------------
# Индекс (singleton: грузим один раз, используем для всех сценариев)
# ---------------------------------------------------------------------------

_index: Index | None = None
_scorer: Scorer | None = None
_files: dict | None = None
_project: str = ""


def _build_index(skeleton: Path, targets: Path, enrich: bool = True) -> None:
    global _index, _scorer, _files, _project, _INFRA_MODULES
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


def predict_scenario(S: dict, no_remote: bool = False) -> dict:
    """Принять сценарий S (dict из scenarios/*.json), вернуть блок edges для submission."""
    sid = S["id"]

    if _use_enhanced:
        # Oracle: создаётся на сценарий, накапливает train_type через carry-over (P-54)
        oracle = Oracle()

        # Шаг 1: найти entry points с oracle carry-over (P-54: disambiguation для шагов без явного train_type)
        entries = enhanced_ground(_index, _scorer, _files, S, oracle=oracle)

        # Шаг 2: BFS-замыкание с Oracle Pruner (remote_call фильтрация по train_type)
        # oracle.train_type накоплен через parse_step в enhanced_ground
        train_type = oracle.train_type
        methods, edges, remote, data = pruned_closure(entries, _index, train_type)
    else:
        train_type = ""
        entries = ground(_index, _scorer, _files, S)
        methods, edges, remote, data = enhanced_closure(entries, _index)

    remote = _filter_remote(remote, no_remote)

    print(
        f"{sid}: входов {len(entries)}, методов {len(methods)}, "
        f"calls {len(edges)}, remote {len(remote)}, data {len(data)}"
        f"  [train={train_type or '-'}]",
        file=sys.stderr,
    )

    return _index.submission(
        sid, methods, edges, remote, data,
        method="nlp+bm25+taxonomy+oracle",
    )


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
    a = ap.parse_args()

    global _use_enhanced
    _use_enhanced = not a.no_phase2

    _build_index(Path(a.skeleton), Path(a.targets), enrich=not a.no_enrich)

    out: dict = {"project": _project, "scenarios": {}}
    for path in a.scenarios:
        S = load_json(path)
        sid = S["id"]
        out["scenarios"][sid] = predict_scenario(S, no_remote=a.no_remote)

    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print(f"сохранено: {a.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
