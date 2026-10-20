#!/usr/bin/env python3
"""Упаковка submission для платформы TechArena.

Формат: ZIP-архив с submission.json в корне + code/ с исходниками.

Использование:
    python scripts/pack.py                   # генерирует submission + пакует
    python scripts/pack.py --no-regen        # только упаковывает (submission.json уже есть)
    python scripts/pack.py -o my-answer.zip  # имя архива явно
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent

# Сценарии в порядке как принято на платформе
SCENARIOS = [f"tt-challenge/scenarios/S{i}.json" for i in range(8)]

# ---------------------------------------------------------------------------
# Используемые файлы (активный pipeline predict.py)
# ---------------------------------------------------------------------------
# Всё что входит сюда обязательно попадает в архив.
# Структура: путь относительно ROOT → путь внутри code/ архива.
USED_FILES: list[str] = [
    # Точка входа предсказания
    "src/predict.py",

    # Enrichment: обогащение BM25-запроса сигнатурами Java-методов
    "src/enrichment/__init__.py",
    "src/enrichment/signatures.py",

    # Core: граф скелета, BM25, NLP, обход файловой системы
    "src/core/__init__.py",
    "src/core/bm25.py",
    "src/core/nlp.py",
    "src/core/skeleton_index.py",
    "src/core/walker.py",

    # Similarity: опциональный BGE-M3 semantic reranker + CodeBERT code encoder + cross-encoder
    "src/similarity/__init__.py",
    "src/similarity/encoder.py",
    "src/similarity/code_encoder.py",
    "src/similarity/cross_encoder.py",

    # Graph: обнаружение сиблинг-пар сервисов (G/OTHER disambiguation)
    "src/graph/__init__.py",
    "src/graph/siblings.py",

    # Localization: entry point поиск + step typing + sibling disambiguation
    "src/localization/__init__.py",
    "src/localization/searcher.py",
    "src/localization/scorer.py",
    "src/localization/service_classifier.py",
    "src/localization/service_matcher.py",
    "src/localization/step_typer.py",
    "src/localization/ui_context.py",
    "src/localization/hub_scorer.py",
    "src/localization/session_context.py",
    "src/localization/flow_graph.py",
    "src/localization/java_context.py",
    "src/localization/qwen_encoder.py",
    "src/localization/passport_generator.py",
    "src/localization/service_router.py",
    "src/localization/fsm_transitions.py",

    # Parsers: Java AST — только Java-парсер нужен для enrichment
    "src/parsers/__init__.py",
    "src/parsers/java.py",

    # Trace: BFS closure + Oracle (carry-over train_type)
    "src/trace/__init__.py",
    "src/trace/closure.py",
    "src/trace/oracle.py",

    # Доменный конфиг и загрузчик (переносимые константы)
    "src/domain_config.yaml",
    "src/_config.py",

    # Конфиг проекта
    "pyproject.toml",
]

# ---------------------------------------------------------------------------
# НЕ ИСПОЛЬЗУЕТСЯ в текущем pipeline (Blast Radius Engine — другая задача)
# ---------------------------------------------------------------------------
# Эти модули входят в репозиторий, но НЕ вызываются из predict.py.
# В submission архив НЕ включаются.
#
# src/__main__.py                         — точка входа старого BRE-пайплайна
#
# src/adapters/                           — адаптеры инструментов (ctags, semgrep,
#   tools/base.py                           docker, nginx, dotenv, yaml, deps)
#   tools/code/{ctags,semgrep,test_adapter}.py
#   tools/config/{deps,docker,dotenv,nginx,properties,yaml_adapter}.py
#   tools/registry.py
#
# src/core/node_id.py                     — NodeId для BRE, не для prediction
# src/core/source_pipeline.py             — pipeline BRE
#
# src/graph/bfs.py                        — compute_blast_radius (BRE)
# src/graph/builder.py                    — GraphBuilder (BRE)
# src/graph/ingester.py                   — FindingIngester (BRE)
# src/graph/node_typing.py               — типизация узлов BRE
# src/graph/package_index.py             — PackageIndex (BRE)
# src/graph/passport.py                  — ServicePassport (BRE)
# src/graph/symbol_index.py              — SymbolIndex (BRE)
#
# src/output/serializer.py               — вывод результатов BRE
#
# src/parsers/base.py                    — абстрактный LanguageParser
# src/parsers/go.py                      — парсер Go (не нужен для train-ticket)
# src/parsers/python.py                  — парсер Python
#
# src/scripts/graph_extractor.py         — утилита извлечения графа
# src/scripts/visualize_graph.py         — визуализация (PyVis)
#
# src/similarity/index.py               — SimilarityIndex (BRE)
#
# src/test/parse_test.py                — тесты парсеров
#
# src/utils/text.py                     — утилиты текста BRE

# ---------------------------------------------------------------------------
# Вспомогательные скрипты (не в USED_FILES, но полезны разработчику)
# ---------------------------------------------------------------------------
# scripts/eval.py    — локальная оценка IoU, не нужна на платформе
# scripts/pack.py    — этот файл

DEV_SCRIPTS: list[str] = [
    "scripts/eval.py",
    "scripts/pack.py",
    "scripts/submit.py",
]

# ---------------------------------------------------------------------------
# Что пропускать внутри включённых деревьев
# ---------------------------------------------------------------------------
SKIP_DIRS  = {"__pycache__", ".mypy_cache", ".ruff_cache", "venv", ".git", "node_modules"}
SKIP_EXTS  = {".pyc", ".pyo", ".egg-info"}
SKIP_FILES = {"_submission.json"}


def generate_submission(out: Path) -> None:
    scenarios_args = [str(ROOT / s) for s in SCENARIOS]
    cmd = [
        sys.executable, str(ROOT / "src" / "predict.py"),
        *scenarios_args,
        "-o", str(out),
    ]
    print(f"Генерирую submission: {out.name} ...", flush=True)
    result = subprocess.run(cmd, cwd=str(ROOT))
    if result.returncode != 0:
        sys.exit(f"predict.py завершился с кодом {result.returncode}")
    print(f"  → {out} ({out.stat().st_size // 1024} КБ)")


def collect_code_files() -> list[tuple[Path, str]]:
    """Список (путь на диске, путь внутри архива code/...) — только используемые файлы."""
    entries: list[tuple[Path, str]] = []
    for rel_str in USED_FILES + DEV_SCRIPTS:
        src = ROOT / rel_str
        if not src.exists():
            print(f"  [WARN] файл не найден: {rel_str}", file=sys.stderr)
            continue
        entries.append((src, f"code/{rel_str}"))
    return entries


CACHE_FILES: list[str] = [
    "cache/service_passports.pkl",
    "cache/qwen_encoder_cache.pkl",
    "cache/ce_scores_cache.pkl",
    "cache/fsm_transitions.json",
]


def build_zip(submission: Path, archive: Path) -> None:
    code_files = collect_code_files()
    cache_files = [(ROOT / f, f"code/{f}") for f in CACHE_FILES if (ROOT / f).exists()]
    if not cache_files:
        print("  [WARN] cache/*.pkl не найдены — Qwen будет генерировать паспорта заново", file=sys.stderr)
    total = len(code_files) + len(cache_files) + 1
    print(f"Упаковываю {len(code_files)} файлов кода + {len(cache_files)} кешей + submission.json → {archive.name}")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(submission, "submission.json")
        for disk_path, arc_name in code_files:
            zf.write(disk_path, arc_name)
        for disk_path, arc_name in cache_files:
            zf.write(disk_path, arc_name)
    size_kb = archive.stat().st_size // 1024
    print(f"  → {archive} ({size_kb} КБ, {total} файлов)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--out", default=None, help="имя ZIP-архива (по умолчанию submission_YYYYMMDD_HHMMSS.zip)")
    ap.add_argument("--no-regen", action="store_true", help="не запускать predict.py, использовать scripts/_submission.json")
    ap.add_argument("--submission", default=None, help="путь к готовому submission.json (с --no-regen)")
    a = ap.parse_args()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive = Path(a.out) if a.out else ROOT / f"submission_{ts}.zip"

    if a.no_regen:
        submission = Path(a.submission) if a.submission else ROOT / "scripts" / "_submission.json"
        if not submission.exists():
            sys.exit(f"Файл не найден: {submission}\nЗапусти без --no-regen или укажи --submission <путь>")
    else:
        submission = ROOT / "scripts" / "_submission.json"
        generate_submission(submission)

    build_zip(submission, archive)
    print(f"\nГотово: {archive}")


if __name__ == "__main__":
    main()
