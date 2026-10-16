"""Фаза 1 — Enrichment: обогащение handler_docs() токенами из исходников.

Строит словарь {node_id → [extra_tokens]} для обработчиков в скелете,
используя logger_tags и flow_steps из Java-исходников.

Использование:
    extras = build_extra_tokens(index, targets_root)
    # затем в predict.py добавляем к code-doc каждого handler
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

_HERE = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_HERE))

from src.core.nlp import toks                        # noqa: E402
from src.core.walker import walk, service_name_of    # noqa: E402
from src.parsers.java import JavaParser              # noqa: E402


_PARSER = JavaParser()


def build_extra_tokens(index, targets_root: Path) -> dict[str, list[str]]:
    """Вернуть {node_id: [дополнительные BM25-токены]} для handler-методов скелета.

    Токены = logger_tags + flow_steps из Java-исходников.
    Привязка: (service, method_name, class_suffix) → NodeId из скелета.
    """
    # Шаг 1: собрать все методы из исходников → (service, class_simple, method) → tokens
    src_tokens: dict[tuple[str, str, str], list[str]] = defaultdict(list)

    for path, lang in walk(targets_root, {"java"}):
        if lang != "java":
            continue
        service = service_name_of(path, targets_root)
        pf = _PARSER.parse(path, service)
        if pf.error:
            continue

        for cls in pf.classes:
            class_simple = cls.fqn.split(".")[-1]  # PreserveServiceImpl
            for mi in cls.methods:
                key = (service, class_simple, mi.name)
                for _, step_text in mi.flow_steps:
                    src_tokens[key].extend(toks(step_text))
                for tag in mi.logger_tags:
                    src_tokens[key].extend(toks(tag))

    print(
        f"[enrichment] Java-файлов: {_count_java(targets_root)}, "
        f"методов с токенами: {len(src_tokens)}",
        file=sys.stderr,
    )

    # Шаг 2: сопоставить с NodeId скелета (только handler-методы для BM25)
    N = index.N
    extra: dict[str, list[str]] = defaultdict(list)
    matched = 0

    for nid, verb, _tpl in index.handlers:
        n = N[nid]
        module: str = n.get("module", "")
        method_name: str = n.get("name", "")
        owner_id: str = n.get("owner", "")
        owner_fqn: str = N[owner_id]["fqn"] if owner_id in N else ""
        class_simple = owner_fqn.split(".")[-1]

        key = (module, class_simple, method_name)
        tokens = src_tokens.get(key, [])

        if not tokens:
            # Fallback: без имени класса — только (service, method)
            for (svc, _cls, mname), tlist in src_tokens.items():
                if svc == module and mname == method_name:
                    tokens = tlist
                    break

        if tokens:
            extra[nid].extend(tokens)
            matched += 1

    print(
        f"[enrichment] обработчиков обогащено: {matched} / {len(index.handlers)}",
        file=sys.stderr,
    )
    return dict(extra)


def _count_java(root: Path) -> int:
    return sum(1 for _ in walk(root, {"java"}))
