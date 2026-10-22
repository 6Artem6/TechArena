"""Bottom-Up Token Clustering: BGE-M3 + AgglomerativeClustering + Qwen + CrossEncoder.

Pipeline:
  1. Collect tokens: module first_tokens, swagger tags, passport synonyms (optional)
  2. BGE-M3 batch encode + L2 normalize
  3. AgglomerativeClustering (metric=cosine, distance_threshold)
  4. Cohesion score per cluster: mean pairwise cosine similarity
     → low_confidence if cohesion < cohesion_threshold
  5. Qwen labeling:
     - normal clusters: токены кластера
     - low_confidence clusters: токены + passport summaries для контекста
  6. Cross-Encoder verification: score_pair(tokens_text, qwen_description)
     → unverified if ce_score < ce_threshold
  7. Output: cache/auto_categories.json с cohesion/ce_score/confidence полями

Usage:
  python scripts/gen_category_clusters.py
  python scripts/gen_category_clusters.py --no-qwen
  python scripts/gen_category_clusters.py --include-synonyms
  python scripts/gen_category_clusters.py --distance-threshold 0.35
  python scripts/gen_category_clusters.py --cohesion-threshold 0.50
"""
from __future__ import annotations

import argparse
import gzip
import json
import pickle
import re
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src._config import cfg_cache_dir

_STOP = frozenset({
    "service", "type", "data", "info", "result", "request", "response",
    "get", "set", "is", "has", "by", "of", "to", "for", "in", "on",
    "and", "or", "the", "a", "an", "with", "all", "new", "do",
    "controller", "impl", "handler", "repository", "mapper", "dto",
    "list", "find", "create", "update", "delete", "query",
    "config", "execute", "admin", "other", "ts", "com",
})


def _load_skeleton(path: Path) -> dict:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path) as f:
        return json.load(f)


def _module_first_tok(module: str) -> str:
    parts = [p for p in module.split("-") if p not in ("ts", "service", "other")]
    return parts[0] if parts else ""


def collect_tokens(
    sk: dict,
    profile_path: Path,
    passports_path: Path,
    include_synonyms: bool,
) -> list[tuple[str, str]]:
    """Вернуть (token, source) из трёх источников."""
    tokens: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(tok: str, src: str) -> None:
        tok = tok.strip().lower()
        if tok and len(tok) > 2 and tok not in _STOP and tok not in seen:
            seen.add(tok)
            tokens.append((tok, src))

    nodes = sk.get("nodes", {})
    for node in nodes.values():
        if node.get("kind") == "module":
            tok = _module_first_tok(node.get("name", ""))
            if tok:
                add(tok, "module")

    if profile_path.exists():
        pp = json.loads(profile_path.read_text(encoding="utf-8"))
        tags = pp.get("tags", [])
        for tag in (tags if isinstance(tags, list) else []):
            for word in re.findall(r"[a-z]+", tag.lower()):
                add(word, "tag")

    if include_synonyms and passports_path.exists():
        passports = pickle.loads(passports_path.read_bytes())
        for methods in passports.values():
            if not isinstance(methods, dict):
                continue
            for info in methods.values():
                if not isinstance(info, dict):
                    continue
                for syn in info.get("synonyms", []):
                    if isinstance(syn, str):
                        for word in re.findall(r"[a-z]+", syn.lower()):
                            add(word, "synonym")

    return tokens


def embed_tokens(token_list: list[str]) -> np.ndarray:
    """BGE-M3 batch encode + L2 normalize."""
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer("BAAI/bge-m3", device="cpu")
    print(f"  BGE-M3 encoding {len(token_list)} tokens...", file=sys.stderr)
    emb = model.encode(token_list, batch_size=64, show_progress_bar=False)
    norms = np.linalg.norm(emb, axis=1, keepdims=True)
    return emb / np.where(norms == 0, 1.0, norms)


def cluster_tokens(embeddings: np.ndarray, distance_threshold: float) -> np.ndarray:
    from sklearn.cluster import AgglomerativeClustering
    return AgglomerativeClustering(
        n_clusters=None,
        distance_threshold=distance_threshold,
        metric="cosine",
        linkage="average",
        compute_full_tree="auto",
    ).fit_predict(embeddings)


def cohesion_score(embeddings: np.ndarray) -> float:
    """Mean pairwise cosine similarity для кластера (L2-норм. → dot product)."""
    if len(embeddings) < 2:
        return 1.0
    # Уже нормализованы — cosine sim = dot product
    sim_matrix = embeddings @ embeddings.T
    n = len(embeddings)
    # Берём только верхний треугольник (без диагонали)
    triu = [(sim_matrix[i, j]) for i in range(n) for j in range(i + 1, n)]
    return float(np.mean(triu))


def build_passport_context(
    tokens: list[str],
    sk: dict,
    passports: dict,
) -> str:
    """Для списка токенов найти паспорта соответствующих модулей.

    token → best matching controller → business_summary методов (топ-3).
    Предпочитаем базовые контроллеры (короткое имя без Admin/Food/Plan префиксов).
    """
    nodes = sk.get("nodes", {})

    # Построить mapping: first_tok → список module names
    tok_to_modules: dict[str, list[str]] = defaultdict(list)
    for node in nodes.values():
        if node.get("kind") == "module":
            mod_name = node.get("name", "")
            first = _module_first_tok(mod_name)
            if first:
                tok_to_modules[first].append(mod_name)

    # Индекс file_key → methods (без хеш-суффикса для совпадения)
    file_to_methods: dict[str, tuple[str, dict]] = {}
    for file_key, methods in passports.items():
        if isinstance(methods, dict):
            fname = file_key.split(":")[0]  # "RouteController.java"
            file_to_methods[fname] = (file_key, methods)

    def _score_filename(fname: str, tok: str) -> int:
        """Меньше = лучше: базовый контроллер без Admin/Food/Plan/Admin суффиксов."""
        base = fname.replace("Controller.java", "").lower()
        penalty = 0
        # Штраф за префиксы/суффиксы которые делают файл специализированным
        for noise in ("admin", "food", "plan", "other", "station", "train"):
            if noise in base and base != tok:
                penalty += 10
        # Бонус: файл = просто {Tok}Controller (идеальное совпадение)
        if base == tok.lower():
            penalty -= 5
        return len(fname) + penalty

    lines: list[str] = []
    for tok in tokens:
        modules = tok_to_modules.get(tok, [])
        mod_label = modules[0] if modules else tok

        # Найти все подходящие файлы (содержат capitalized tok)
        candidates = [
            (fname, fk, methods)
            for fname, (fk, methods) in file_to_methods.items()
            if tok.capitalize() in fname
        ]
        if not candidates:
            continue
        # Выбрать наиболее базовый: минимальный score
        candidates.sort(key=lambda x: _score_filename(x[0], tok))
        fname, _, methods = candidates[0]

        summaries = []
        for method_name, info in list(methods.items())[:3]:
            if isinstance(info, dict):
                s = info.get("business_summary", "")
                if s and method_name != "home":
                    summaries.append(f"  - {method_name}: {s}")
        if summaries:
            lines.append(f"Module '{tok}' ({mod_label}, via {fname}):")
            lines.extend(summaries)

    return "\n".join(lines) if lines else ""


def label_cluster_qwen(
    tokens: list[str], enc, context: str = "", project_context: str = ""
) -> dict:
    """Qwen: назвать кластер.

    project_context — домен/фичи проекта из project_passport.json (глобальный).
    context — паспорта конкретных сервисов для low_confidence кластеров (кластерный).
    """
    token_str = json.dumps(tokens, ensure_ascii=False)
    proj_block = (
        f"\nProject background:\n{project_context}\n" if project_context else ""
    )
    ctx_block = f"\nAdditional context from service passports:\n{context}\n" if context else ""
    prompt = (
        f"I have a cluster of code tokens from a microservice codebase: {token_str}"
        f"{proj_block}"
        f"{ctx_block}\n"
        "Identify the business domain these tokens represent.\n"
        "Respond with JSON only:\n"
        '{"category_id": "snake_case_name", "description": "one sentence", '
        '"patterns": ["\\\\btoken1\\\\b", "\\\\btoken2\\\\b"]}'
    )
    schema = {
        "type": "object",
        "properties": {
            "category_id": {"type": "string"},
            "description": {"type": "string"},
            "patterns": {"type": "array", "items": {"type": "string"}},
        },
    }
    try:
        return enc._call_ollama(prompt, schema=schema, num_predict=200, timeout=30)
    except Exception as e:
        print(f"  [qwen] failed: {e}", file=sys.stderr)
        return {}


def verify_with_cross_encoder(
    tokens: list[str],
    description: str,
    ce,
) -> float:
    """score_pair(tokens_text, description) → [0, 1]. >0.5 = verified."""
    tokens_text = " ".join(tokens)
    return ce.score_pair(tokens_text, description)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skeleton", default="tt-challenge/skeleton/trainticket.json.gz")
    parser.add_argument("--no-qwen", action="store_true")
    parser.add_argument("--no-ce", action="store_true", help="Пропустить Cross-Encoder верификацию")
    parser.add_argument("--include-synonyms", action="store_true")
    parser.add_argument("--distance-threshold", type=float, default=0.40)
    parser.add_argument("--cohesion-threshold", type=float, default=0.55,
                        help="Кластеры с cohesion < threshold → low_confidence → passport enrichment")
    parser.add_argument("--ce-threshold", type=float, default=0.50,
                        help="CE score < threshold → кластер помечается unverified")
    parser.add_argument("--min-cluster-size", type=int, default=2)
    args = parser.parse_args()

    sk_path = ROOT / args.skeleton
    if not sk_path.exists():
        print(f"[ERROR] skeleton not found: {sk_path}", file=sys.stderr)
        sys.exit(1)

    cache_dir = cfg_cache_dir()
    profile_path = cache_dir / "project_profile.json"
    passports_path = cache_dir / "service_passports.pkl"

    # --- Project Passport Context ---
    project_context = ""
    passport_path = cache_dir / "project_passport.json"
    if passport_path.exists():
        try:
            pp = json.loads(passport_path.read_text(encoding="utf-8"))
            domain = pp.get("domain", "")
            features = pp.get("main_features", [])
            journeys = pp.get("user_journeys", [])
            parts = []
            if domain:
                parts.append(f"Domain: {domain}")
            if features:
                parts.append("Features: " + "; ".join(features))
            if journeys:
                parts.append("User journeys: " + "; ".join(journeys))
            project_context = "\n".join(parts)
            print(f"Project passport loaded: {domain[:60]}", file=sys.stderr)
        except Exception as e:
            print(f"  [warn] project_passport.json read failed: {e}", file=sys.stderr)
    else:
        print("project_passport.json не найден — запустите gen_project_passport.py",
              file=sys.stderr)

    # --- Этап 1: Сбор токенов ---
    print("Сбор токенов...", file=sys.stderr)
    sk = _load_skeleton(sk_path)
    pairs = collect_tokens(sk, profile_path, passports_path, args.include_synonyms)
    tokens = [p[0] for p in pairs]
    sources = [p[1] for p in pairs]
    print(f"  {len(tokens)} токенов "
          f"(module={sources.count('module')}, tag={sources.count('tag')}, "
          f"synonym={sources.count('synonym')})", file=sys.stderr)

    if len(tokens) < 2:
        print("[ERROR] слишком мало токенов для кластеризации", file=sys.stderr)
        sys.exit(1)

    # --- Этап 2: Векторизация ---
    embeddings = embed_tokens(tokens)
    tok_to_emb = {tok: embeddings[i] for i, tok in enumerate(tokens)}

    # --- Этап 3: Кластеризация ---
    print(f"Кластеризация (distance_threshold={args.distance_threshold})...", file=sys.stderr)
    labels = cluster_tokens(embeddings, args.distance_threshold)
    n_clusters = len(set(labels))
    print(f"  {n_clusters} кластеров найдено", file=sys.stderr)

    # --- Этап 4: Группировка + Cohesion ---
    cluster_map: dict[int, list[tuple[str, str]]] = defaultdict(list)
    for tok, src, label in zip(tokens, sources, labels):
        cluster_map[label].append((tok, src))

    clusters: dict[int, dict] = {}
    for lbl, toks in cluster_map.items():
        if len(toks) < args.min_cluster_size:
            continue
        tok_list = [t for t, _ in toks]
        embs = np.array([tok_to_emb[t] for t in tok_list])
        coh = cohesion_score(embs)
        low_conf = coh < args.cohesion_threshold
        clusters[lbl] = {
            "tokens": tok_list,
            "sources": [s for _, s in toks],
            "cohesion": round(coh, 3),
            "low_confidence": low_conf,
        }

    print(f"  {len(clusters)} кластеров ≥ {args.min_cluster_size} токенов:", file=sys.stderr)
    for lbl, info in sorted(clusters.items(), key=lambda x: -len(x[1]["tokens"])):
        flag = " ⚠ low_conf" if info["low_confidence"] else ""
        print(f"  #{lbl:3d} coh={info['cohesion']:.2f}{flag}: {info['tokens']}", file=sys.stderr)

    # --- Этап 5: Паспорта для low_confidence ---
    passports: dict = {}
    if passports_path.exists():
        passports = pickle.loads(passports_path.read_bytes())

    # --- Этап 6: Qwen Labeling ---
    if args.no_qwen:
        print("\n--no-qwen: пропуск labeling", file=sys.stderr)
        for lbl, info in clusters.items():
            info.update({"category_id": None, "description": None, "patterns": [],
                         "passport_enriched": False, "ce_score": None, "verified": None})
    else:
        from src.localization.qwen_encoder import QwenEncoder
        enc = QwenEncoder()
        print(f"\nQwen labeling {len(clusters)} кластеров...", file=sys.stderr)
        for lbl, info in sorted(clusters.items(), key=lambda x: -len(x[1]["tokens"])):
            tok_list = info["tokens"]
            context = ""
            if info["low_confidence"]:
                context = build_passport_context(tok_list, sk, passports)
                if context:
                    print(f"  #{lbl} [low_conf] → passport context добавлен", file=sys.stderr)
            labeled = label_cluster_qwen(tok_list, enc, context=context,
                                         project_context=project_context)
            info["category_id"] = labeled.get("category_id")
            info["description"] = labeled.get("description")
            info["patterns"] = labeled.get("patterns", [])
            info["passport_enriched"] = bool(context)
            print(f"  #{lbl:3d} → {info['category_id']}: {(info['description'] or '')[:60]}",
                  file=sys.stderr)

    # --- Этап 7: Cross-Encoder верификация + Passport Retry ---
    if not args.no_qwen and not args.no_ce:
        print("\nCross-Encoder верификация...", file=sys.stderr)
        from src.similarity.cross_encoder import CrossEncoder
        ce = CrossEncoder()
        if not ce.enabled:
            print("  [CE] модель не загружена — пропуск", file=sys.stderr)
            for info in clusters.values():
                info["ce_score"] = None
                info["verified"] = None
        else:
            # Первый прогон CE
            for lbl, info in sorted(clusters.items(), key=lambda x: -len(x[1]["tokens"])):
                desc = info.get("description") or info.get("category_id") or ""
                if not desc:
                    info["ce_score"] = None
                    info["verified"] = None
                    continue
                ce_score = verify_with_cross_encoder(info["tokens"], desc, ce)
                verified = ce_score >= args.ce_threshold
                info["ce_score"] = round(ce_score, 3)
                info["verified"] = verified
                status = "✓" if verified else "✗ UNVERIFIED"
                print(f"  #{lbl:3d} CE={ce_score:.3f} {status}: {info.get('category_id')}",
                      file=sys.stderr)

            # Retry для unverified: passport enrichment → Qwen → CE снова
            unverified = [lbl for lbl, info in clusters.items() if info.get("verified") is False]
            if unverified:
                from src.localization.qwen_encoder import QwenEncoder
                enc_retry = QwenEncoder()
                print(f"\nPassport retry для {len(unverified)} unverified кластеров...",
                      file=sys.stderr)
                for lbl in unverified:
                    info = clusters[lbl]
                    tok_list = info["tokens"]
                    context = build_passport_context(tok_list, sk, passports)
                    if context:
                        print(f"  #{lbl:3d} passport context:\n{context}", file=sys.stderr)
                    else:
                        print(f"  #{lbl:3d} passport context пуст — повтор без контекста",
                              file=sys.stderr)
                    labeled = label_cluster_qwen(tok_list, enc_retry, context=context,
                                                 project_context=project_context)
                    new_cat = labeled.get("category_id")
                    new_desc = labeled.get("description")
                    new_pats = labeled.get("patterns", [])
                    if new_cat:
                        ce_score = verify_with_cross_encoder(tok_list, new_desc or new_cat, ce)
                        verified = ce_score >= args.ce_threshold
                        info.update({
                            "category_id": new_cat,
                            "description": new_desc,
                            "patterns": new_pats,
                            "passport_enriched": bool(context),
                            "ce_score": round(ce_score, 3),
                            "verified": verified,
                        })
                        status = "✓" if verified else "✗ still unverified"
                        print(f"  #{lbl:3d} retry CE={ce_score:.3f} {status}: {new_cat}",
                              file=sys.stderr)
    elif args.no_ce:
        for info in clusters.values():
            info.setdefault("ce_score", None)
            info.setdefault("verified", None)

    # --- Сохранение ---
    results = [
        {"cluster_id": int(lbl), **info}
        for lbl, info in sorted(clusters.items(), key=lambda x: -len(x[1]["tokens"]))
    ]
    out_path = cache_dir / "auto_categories.json"
    out_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nСохранено {len(results)} кластеров → {out_path}", file=sys.stderr)

    if not args.no_qwen:
        verified = sum(1 for r in results if r.get("verified") is True)
        unverified = sum(1 for r in results if r.get("verified") is False)
        low_conf = sum(1 for r in results if r.get("low_confidence"))
        print(f"  verified={verified}, unverified={unverified}, low_conf={low_conf}",
              file=sys.stderr)


if __name__ == "__main__":
    main()
