"""Генерация cache/step_token_bridges.pkl — BM25 co-occurrence маппинг step_type → tokens.

Алгоритм:
  1. Загрузить скелет → Index → handler_docs → Scorer (BM25)
  2. Из qwen_encoder_cache.pkl взять cls_v3 записи → сгруппировать по step_type
  3. Для каждого step_type: BM25 query по action текстам → top handlers → name tokens
  4. Сохранить {step_type: [tokens]} в cache/step_token_bridges.pkl

Результат используется в step_typer._build_extra_tokens() вместо вручных
token_injection_rules по step_type. Атрибутные маппинги (seat_class, has_food)
остаются в token_injection_rules.

Portable: никакого trainticket-хардкода — данные полностью из скелета и кеша.
"""
from __future__ import annotations

import argparse
import collections
import gzip
import json
import pickle
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src._config import cfg_cache_dir
from src.core.skeleton_index import Index
from src.core.nlp import ntoks
from src.localization.ui_context import handler_docs


# Стоп-токены которые появляются в именах почти всех handlers — информации не несут
_BRIDGE_STOP = frozenset({
    "get", "set", "is", "has", "by", "of", "to", "for", "in", "on",
    "and", "or", "the", "a", "an", "with", "all", "new", "do",
    "service", "controller", "impl", "info", "result", "response",
    "request", "list", "find", "create", "update", "delete",
})


def _load_skeleton(path: Path) -> dict:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path) as f:
        return json.load(f)


def _load_cls_v3_cache() -> dict[str, list[tuple[str, list[str]]]]:
    """Из qwen_encoder_cache.pkl извлечь cls_v3 → {step_type: [(action, keywords), ...]}."""
    cache_path = cfg_cache_dir() / "qwen_encoder_cache.pkl"
    if not cache_path.exists():
        return {}
    cache = pickle.loads(cache_path.read_bytes())
    step_actions: dict[str, list[tuple[str, list[str]]]] = collections.defaultdict(list)
    for key, val in cache.items():
        if not (isinstance(key, tuple) and key[0] == "cls_v3"):
            continue
        action = key[1]
        step_type = getattr(val, "step_type", "") or ""
        keywords = getattr(val, "keywords", []) or []
        if step_type and step_type not in ("other", "unknown", ""):
            step_actions[step_type].append((action, keywords))
    return dict(step_actions)


def _query_tokens(action: str) -> list[str]:
    """Простая токенизация action текста для BM25 запроса."""
    import re
    words = re.findall(r"[a-z]+", action.lower())
    return [w for w in words if len(w) > 2]


def _module_first_tok(module: str) -> str:
    """ts-execute-service → 'execute', ts-order-other-service → 'order'."""
    parts = [p for p in module.split("-") if p not in ("ts", "service", "other")]
    return parts[0] if parts else ""


def _extract_bridge_tokens(
    ranked: list[tuple[float, str]],
    ix: Index,
    top_k: int = 5,
) -> list[str]:
    """Из top-k ranked handlers извлечь информативные токены.

    Приоритет: module first_token (надёжнее, отражает домен) × 2,
    затем name tokens × 1. Это предотвращает ситуацию когда generic
    handler name ("check", "get") перебивает нужный модульный сигнал.
    """
    mod_freq: dict[str, int] = collections.Counter()
    name_freq: dict[str, int] = collections.Counter()

    for _score, nid in ranked[:top_k]:
        node = ix.N.get(nid)
        if not node:
            continue
        mod_tok = _module_first_tok(node.get("module", ""))
        if mod_tok and mod_tok not in _BRIDGE_STOP and len(mod_tok) > 2:
            mod_freq[mod_tok] += 1
        for t in ntoks(node.get("name", "")):
            if t not in _BRIDGE_STOP and len(t) > 2:
                name_freq[t] += 1

    result = []
    # Module tokens с весом × 2 (выше надёжность)
    for tok, cnt in mod_freq.most_common(3):
        result.extend([tok] * min(cnt * 2, 4))
    # Name tokens — только если не дублируют module tokens
    existing = set(result)
    for tok, cnt in name_freq.most_common(4):
        if tok not in existing and len(result) < 8:
            result.extend([tok] * min(cnt, 2))
    return result[:8]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skeleton", default="tt-challenge/skeleton/trainticket.json.gz")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--min-examples", type=int, default=1)
    args = parser.parse_args()

    sk_path = ROOT / args.skeleton
    if not sk_path.exists():
        print(f"[ERROR] skeleton not found: {sk_path}", file=sys.stderr)
        sys.exit(1)

    print("Загрузка скелета...", file=sys.stderr)
    sk = _load_skeleton(sk_path)
    ix = Index(sk)

    print("Построение BM25 индекса handlers...", file=sys.stderr)
    code_docs, _ui = handler_docs(ix, {})

    # Enrich doc_texts словами из service passports (NL-описания методов)
    passports_path = cfg_cache_dir() / "service_passports.pkl"
    if passports_path.exists():
        import pickle as _pickle
        import re as _re
        passports = _pickle.loads(passports_path.read_bytes())
        # Построить nid → метод-словарь по имени controller файла
        file_to_nids: dict[str, dict[str, str]] = collections.defaultdict(dict)
        for nid, node in ix.N.items():
            if node.get("kind") == "method" and node.get("module", "").startswith("ts-"):
                fname = node.get("owner", "").split(":")[-1].split(".")[-1] + ".java"
                file_to_nids[fname][node["name"]] = nid

        passport_toks_added = 0
        for file_key, methods in passports.items():
            fname = file_key.split(":")[0]
            for method_name, info in methods.items():
                nid = file_to_nids.get(fname, {}).get(method_name)
                if not nid or nid not in code_docs:
                    continue
                summary = info.get("business_summary", "") if isinstance(info, dict) else ""
                synonyms = info.get("synonyms", []) if isinstance(info, dict) else []
                extra = ntoks(summary) + [s for s in synonyms if isinstance(s, str)]
                extra = [t for t in extra if len(t) > 2 and t not in _BRIDGE_STOP]
                if extra:
                    code_docs[nid] = code_docs[nid] + extra
                    passport_toks_added += 1
        print(f"  Passport enrichment: +{passport_toks_added} методов", file=sys.stderr)

    from src.localization.scorer import Scorer
    scorer = Scorer(code_docs, {})
    print(f"  {len(code_docs)} handlers в индексе", file=sys.stderr)

    print("Загрузка cls_v3 кеша...", file=sys.stderr)
    step_actions = _load_cls_v3_cache()
    if not step_actions:
        print("[ERROR] qwen_encoder_cache.pkl пуст или отсутствует", file=sys.stderr)
        sys.exit(1)
    print(f"  {len(step_actions)} step_types в кеше", file=sys.stderr)

    bridges: dict[str, list[str]] = {}

    for step_type, actions in sorted(step_actions.items()):
        if len(actions) < args.min_examples:
            continue

        all_ranked: list[tuple[float, str]] = []
        # step_type seed: "check_price" → ["check","price"] — code-level контекст
        step_seed = [t for t in ntoks(step_type.replace("_", " ")) if len(t) > 2] * 3
        for action, keywords in actions[:5]:  # не более 5 примеров per step_type
            kw_toks = [t for t in keywords if isinstance(t, str) and len(t) > 2] * 2
            q = _query_tokens(action) + step_seed + kw_toks
            if not q:
                continue
            ranked = scorer.rank(q)
            all_ranked.extend(ranked[:args.top_k])

        if not all_ranked:
            continue

        # Агрегировать: суммировать score по handler
        score_sum: dict[str, float] = collections.defaultdict(float)
        for score, nid in all_ranked:
            score_sum[nid] += score
        sorted_handlers = sorted(score_sum.items(), key=lambda x: -x[1])

        toks = _extract_bridge_tokens(
            [(s, nid) for nid, s in sorted_handlers],
            ix,
            top_k=args.top_k,
        )
        bridges[step_type] = toks
        print(f"  {step_type:20s} → {toks}", file=sys.stderr)

    out = cfg_cache_dir() / "step_token_bridges.pkl"
    out.write_bytes(pickle.dumps(bridges))
    print(f"\nСохранено {len(bridges)} bridges → {out}", file=sys.stderr)

    # Также сохранить JSON для инспекции
    out_json = cfg_cache_dir() / "step_token_bridges.json"
    out_json.write_text(json.dumps(bridges, indent=2, ensure_ascii=False))
    print(f"JSON для инспекции: {out_json}", file=sys.stderr)


if __name__ == "__main__":
    main()
