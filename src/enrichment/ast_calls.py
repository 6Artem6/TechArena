"""Патч call-рёбер из тел методов через tree-sitter AST.

Скелет пропускает вызовы из конструкторов через enum-поля и цепочки доступа:
  Order#<init>() → OrderStatus#getCode()   (OrderStatus.PAID.getCode())
  Order#<init>() → SeatClass#getCode()     (SeatClass.FIRSTCLASS.getCode())

Добавляем найденные рёбра в ix.calls перед BFS-замыканием.
Только однозначные разрешения (один кандидат на (class_simple, method_name)).
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

import tree_sitter_java as tsjava
from tree_sitter import Language, Parser

if TYPE_CHECKING:
    from ..core.skeleton_index import Index

_JAVA_LANG = Language(tsjava.language())
_PARSER = Parser(_JAVA_LANG)


def _node_text(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _receiver_class(obj_node, src: bytes) -> str:
    """Извлечь simple class name из объекта method_invocation.

    Обрабатывает:
      SomeClass.method()          → "SomeClass"
      SomeClass.FIELD.method()    → "SomeClass"  (enum/static field access)
      SomeClass.a.b.method()      → "SomeClass"
    Пропускает:
      someVar.method()            → "" (lowercase — переменная)
      this.method()               → ""
    """
    if obj_node is None:
        return ""
    t = obj_node.type
    if t == "identifier":
        name = _node_text(obj_node, src)
        return name if name and name[0].isupper() else ""
    if t == "field_access":
        inner = obj_node.child_by_field_name("object")
        if inner is not None:
            return _receiver_class(inner, src)
    return ""


def _collect_invocations(root, src: bytes) -> list[tuple[int, str, str]]:
    """Обойти дерево и собрать (line_1based, receiver_class, method_name).

    Только вызовы вида ClassName(.FIELD)*.method() — uppercase-квалификатор.
    """
    results: list[tuple[int, str, str]] = []

    def walk(node) -> None:
        if node.type == "method_invocation":
            obj = node.child_by_field_name("object")
            name_node = node.child_by_field_name("name")
            if name_node and obj is not None:
                cls = _receiver_class(obj, src)
                if cls:
                    line = src[: node.start_byte].count(b"\n") + 1
                    results.append((line, cls, _node_text(name_node, src)))
        for child in node.children:
            walk(child)

    walk(root)
    return results


def patch_calls(ix: "Index", targets_root: Path) -> int:
    """Дописать рёбра calls в ix.calls из AST тел методов.

    Returns: количество добавленных рёбер.
    """
    # Lookup: (class_simple, method_name) → {nid}
    key_to_nids: dict[tuple[str, str], set[str]] = defaultdict(set)
    for nid, nd in ix.N.items():
        if nd.get("kind") != "method":
            continue
        owner_nd = ix.N.get(nd.get("owner", ""), {})
        fqn = owner_nd.get("fqn", "")
        cls_simple = fqn.split(".")[-1] if fqn else ""
        if not cls_simple:
            parts = nid.split(":")
            if len(parts) >= 3:
                cls_simple = parts[2].split(".")[-1].split("#")[0]
        method = nd.get("name", "")
        if cls_simple and method:
            key_to_nids[(cls_simple, method)].add(nid)

    # Сгруппировать nid-ы скелета по файлу
    by_file: dict[str, list[tuple[str, int, int]]] = defaultdict(list)
    for nid, nd in ix.N.items():
        if nd.get("kind") != "method":
            continue
        f = nd.get("file", "")
        line = nd.get("line")
        end = nd.get("end")
        if f and line and end:
            by_file[f].append((nid, line, end))

    added = 0

    for file_rel, entries in by_file.items():
        fpath = targets_root / file_rel
        try:
            src = fpath.read_bytes()
        except OSError:
            continue

        try:
            tree = _PARSER.parse(src)
        except Exception:
            continue

        # Все method_invocations файла с номерами строк — разбираем файл один раз
        all_inv = _collect_invocations(tree.root_node, src)
        if not all_inv:
            continue

        for src_nid, line_s, line_e in entries:
            # Фильтр по диапазону строк метода
            relevant = [
                (cls, meth)
                for (line, cls, meth) in all_inv
                if line_s <= line <= line_e
            ]
            if not relevant:
                continue

            existing = ix.calls.get(src_nid, set())

            for cls_simple, method_name in relevant:
                tgt_set = key_to_nids.get((cls_simple, method_name))
                if not tgt_set or len(tgt_set) != 1:
                    continue  # пропустить неоднозначное или отсутствующее
                tgt_nid = next(iter(tgt_set))
                if tgt_nid == src_nid or tgt_nid in existing:
                    continue
                ix.calls[src_nid].add(tgt_nid)
                existing.add(tgt_nid)
                added += 1

    return added
