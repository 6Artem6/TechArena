"""Офлайн генерация семантических паспортов для REST endpoint-ов.

Для каждого контроллера посылает в Qwen один запрос → business_summary + synonyms.
Паспорта кешируются в ~/.cache/service_passports.json (ключ: filename:md5).

Использование:
    passports = load_or_generate_passports(ix, targets_root, qwen_encoder)
    # → {nid: PassportData(business_summary, synonyms)}
"""
from __future__ import annotations

import hashlib
import json
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from src.core.skeleton_index import Index
    from src.localization.qwen_encoder import QwenEncoder

_CACHE_FILE = Path.home() / ".cache" / "service_passports.json"
_MAX_SRC_CHARS = 6000   # обрезать файл при превышении
_NUM_PREDICT   = 600    # достаточно для 4-8 endpoint-ов


# ---------------------------------------------------------------------------
# Данные паспорта одного handler-а
# ---------------------------------------------------------------------------

class PassportData(NamedTuple):
    business_summary: str
    synonyms: list[str]


# ---------------------------------------------------------------------------
# Промпт для Qwen
# ---------------------------------------------------------------------------

_PASSPORT_SYSTEM = (
    "You are generating semantic descriptions for REST API endpoints.\n"
    "Given Java controller source code, for each @GetMapping/@PostMapping/"
    "@PutMapping/@DeleteMapping method return a JSON object.\n\n"
    "Fields per endpoint:\n"
    "  method_name: exact Java method name\n"
    "  business_summary: 1 sentence — what a USER does when calling this endpoint\n"
    "  synonyms: 5-8 natural language phrases a user might say (NOT Java identifiers)\n\n"
    "CRITICAL:\n"
    "  - synonyms MUST distinguish THIS endpoint from others in the same controller\n"
    "  - search variants: list specific criteria (cheapest/quickest/transfer/fewest stops)\n"
    "  - 'transfer'/'transit'/'layover' ONLY for endpoints that handle multi-leg journeys\n"
    "  - admin actions: use admin verbs (add station, add route, configure, manage)\n\n"
    "Example output:\n"
    '{"endpoints": [\n'
    '  {"method_name": "getTransferResult",\n'
    '   "business_summary": "Find train routes requiring a transfer at an intermediate station.",\n'
    '   "synonyms": ["transit", "transfer", "layover", "connection", "via", '
    '"indirect route", "change trains", "intermediate stop"]},\n'
    '  {"method_name": "getByCheapest",\n'
    '   "business_summary": "Find the cheapest direct train between two stations.",\n'
    '   "synonyms": ["cheapest", "lowest price", "budget", "least expensive", '
    '"minimum cost", "affordable"]}\n'
    "]}\n"
    "Output ONLY valid JSON. No other text."
)

_PASSPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "endpoints": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "method_name":       {"type": "string"},
                    "business_summary":  {"type": "string"},
                    "synonyms":          {"type": "array", "items": {"type": "string"}},
                },
                "required": ["method_name", "business_summary", "synonyms"],
            },
        }
    },
    "required": ["endpoints"],
}


# ---------------------------------------------------------------------------
# Генерация для одного файла
# ---------------------------------------------------------------------------

def _file_hash(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()[:12]


def _generate_for_file(
    src_path: Path,
    valid_method_names: set[str],
    qwen: "QwenEncoder",
) -> dict[str, PassportData]:
    """Один Qwen-вызов для всего контроллера → {method_name: PassportData}."""
    src_text = src_path.read_text(errors="ignore")[:_MAX_SRC_CHARS]
    prompt = f"Controller source:\n```java\n{src_text}\n```"

    try:
        raw = qwen._call_ollama(
            prompt=prompt,
            schema=_PASSPORT_SCHEMA,
            system=_PASSPORT_SYSTEM,
            num_predict=_NUM_PREDICT,
            timeout=300,
        )
    except Exception as exc:
        print(f"[passport] ERROR {src_path.name}: {exc}", file=sys.stderr)
        return {}

    result: dict[str, PassportData] = {}
    for ep in raw.get("endpoints", []):
        mname = ep.get("method_name", "").strip()
        if mname not in valid_method_names:
            continue
        summary  = str(ep.get("business_summary", "")).strip()
        synonyms = [str(s).strip().lower() for s in ep.get("synonyms", []) if s][:10]
        if summary or synonyms:
            result[mname] = PassportData(business_summary=summary, synonyms=synonyms)

    return result


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------

def load_or_generate_passports(
    ix: "Index",
    targets_root: "Path | None",
    qwen: "QwenEncoder",
    n_workers: int = 4,
) -> dict[str, PassportData]:
    """Загрузить из кеша или сгенерировать паспорта.

    Возвращает {nid: PassportData}.
    Пустой dict если Qwen недоступен или targets_root не задан.
    """
    if targets_root is None or not qwen.enabled:
        return {}

    targets_root = Path(targets_root)

    # java_files: filename → Path
    java_files: dict[str, Path] = {}
    for p in targets_root.rglob("*.java"):
        java_files[p.name] = p

    # by_file: filename → [(nid, method_name)]
    by_file: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for nid, _verb, _tpl in ix.handlers:
        n = ix.N.get(nid, {})
        fname = Path(n.get("file", "")).name
        mname = n.get("name", "")
        if fname and mname and fname in java_files:
            by_file[fname].append((nid, mname))

    # Загрузить кеш
    cache: dict[str, dict] = {}
    if _CACHE_FILE.exists():
        try:
            cache = json.loads(_CACHE_FILE.read_text())
        except Exception:
            cache = {}

    nid_results: dict[str, PassportData] = {}
    files_to_generate: list[tuple[str, Path, list[tuple[str, str]], str]] = []

    for fname, entries in by_file.items():
        src_path = java_files.get(fname)
        if src_path is None:
            continue
        fhash    = _file_hash(src_path)
        cache_key = f"{fname}:{fhash}"

        if cache_key in cache:
            cached = cache[cache_key]
            for nid, mname in entries:
                if mname in cached:
                    ep = cached[mname]
                    nid_results[nid] = PassportData(
                        business_summary=ep.get("business_summary", ""),
                        synonyms=ep.get("synonyms", []),
                    )
        else:
            files_to_generate.append((fname, src_path, entries, cache_key))

    if files_to_generate:
        print(
            f"[passport] generating {len(files_to_generate)} files "
            f"({n_workers} workers)...",
            file=sys.stderr,
        )

        def _process(item):
            fname, src_path, entries, cache_key = item
            valid_names = {m for _, m in entries}
            passports = _generate_for_file(src_path, valid_names, qwen)
            return fname, entries, cache_key, passports

        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            futs = {pool.submit(_process, item): item for item in files_to_generate}
            for fut in as_completed(futs):
                try:
                    fname, entries, cache_key, passports = fut.result()
                    # Кешируем только если получили хотя бы 1 endpoint.
                    # Пустые результаты (timeout/ошибка) не кешируем → будут повторены.
                    if passports:
                        cache[cache_key] = {
                            m: {"business_summary": p.business_summary, "synonyms": p.synonyms}
                            for m, p in passports.items()
                        }
                    for nid, mname in entries:
                        if mname in passports:
                            nid_results[nid] = passports[mname]
                    print(
                        f"[passport]   {fname}: {len(passports)} endpoints",
                        file=sys.stderr,
                    )
                except Exception as exc:
                    print(f"[passport] worker error: {exc}", file=sys.stderr)

        try:
            _CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=1))
        except Exception as exc:
            print(f"[passport] cache save error: {exc}", file=sys.stderr)

    print(
        f"[passport] {len(nid_results)} handler passports loaded",
        file=sys.stderr,
    )
    return nid_results
