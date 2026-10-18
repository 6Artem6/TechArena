"""Иерархический CE контекст из Java исходников.

Уровни (сервис → класс → метод):
  - module (ts-xxx-service)
  - class name + все методы класса как контекст
  - метод: javadoc, logger messages, flow_steps, line comments, @PathVariable имена

Используется ТОЛЬКО для CrossEncoder (не для BGE-M3/BM25) — богатый текст
позволяет CE сопоставлять NL шаг ("cancel the ticket") с log-строкой
("[Cancel Ticket, Verify Success]") без хардкода.
"""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core.skeleton_index import Index

# Javadoc /** ... */
_JAVADOC_RE = re.compile(r'/\*\*(.*?)\*/', re.DOTALL)

# LOGGER.*("...") — весь строковый первый аргумент
_LOGGER_MSG_RE = re.compile(
    r'(?:LOGGER|log|LOG)\s*\.\s*(?:info|warn|error|debug|trace)\s*\(\s*"([^"]*)"'
)

# System.out.println("...")
_PRINTLN_RE = re.compile(r'System\.out\.println\s*\(\s*"([^"]+)"')

# //N. flow step
_FLOW_STEP_RE = re.compile(r'//\s*\d+\.\s*(.{4,120})', re.MULTILINE)

# // обычный комментарий (не flow step, не TODO/FIXME)
_LINE_COMMENT_RE = re.compile(
    r'//\s*(?!\d+\.)(?!TODO|FIXME|XXX|BUG|HACK)(.{4,100})', re.MULTILINE
)

# @PathVariable / @RequestParam имена переменных
_PATH_VAR_RE = re.compile(r'@(?:PathVariable|RequestParam)[^)]*\)\s*\w+\s+(\w+)')
_PATH_VAR_SIMPLE_RE = re.compile(r'@(?:PathVariable|RequestParam)\s+\w+\s+(\w+)')

# @RequestBody тип и переменная
_REQUEST_BODY_RE = re.compile(r'@RequestBody\s+(\w[\w<>,\s]*?)\s+(\w+)\s*[,)]')


def _extract_javadoc(pre_text: str) -> str:
    """Извлечь и очистить последний Javadoc блок перед методом."""
    matches = list(_JAVADOC_RE.finditer(pre_text))
    if not matches:
        return ''
    raw = matches[-1].group(1)
    lines = []
    for line in raw.splitlines():
        line = line.strip().lstrip('*').strip()
        if line.startswith('@param'):
            parts = line.split(None, 2)
            if len(parts) >= 3:
                lines.append(parts[2])
        elif line.startswith('@return'):
            parts = line.split(None, 1)
            if len(parts) == 2:
                lines.append(parts[1])
        elif line.startswith('@'):
            pass
        elif line:
            lines.append(line)
    return ' '.join(lines)[:300]


def build_java_context(ix: "Index", targets_root: "Path | None") -> dict[str, str]:
    """Построить CE-специфичный doc_text для каждого handler.

    Returns:
        {nid: rich_text} где rich_text объединяет:
          - verb + path + name + class + module (структура)
          - javadoc + logger messages + flow_steps + comments (семантика)
          - имена соседних методов класса (контекст класса)
    """
    if targets_root is None:
        return {}

    targets_root = Path(targets_root)

    # Mapping: basename → Path (один раз)
    java_files: dict[str, Path] = {}
    for p in targets_root.rglob('*.java'):
        java_files[p.name] = p

    # Группировать handlers по файлу (fname → [(nid, line, end)])
    by_file: dict[str, list[tuple[str, int, int]]] = defaultdict(list)
    for nid, _verb, _tpl in ix.handlers:
        n = ix.N.get(nid, {})
        f = n.get('file', '')
        line = n.get('line')
        end_line = n.get('end')
        if f and line and end_line:
            by_file[Path(f).name].append((nid, line, end_line))

    # Mapping: owner_nid → [handler_nids] для контекста класса
    by_class: dict[str, list[str]] = defaultdict(list)
    for nid, _verb, _tpl in ix.handlers:
        owner = ix.N.get(nid, {}).get('owner', '')
        if owner:
            by_class[owner].append(nid)

    docs: dict[str, str] = {}

    for fname, entries in by_file.items():
        src_path = java_files.get(fname)
        if src_path is None:
            continue
        try:
            src_text = src_path.read_text(errors='ignore')
            src_lines = src_text.splitlines()
        except Exception:
            continue

        for nid, line, end_line in entries:
            n = ix.N.get(nid, {})
            name = n.get('name', '')
            module = n.get('module', ix.module.get(nid, ''))
            owner_id = n.get('owner', '')

            class_simple = ''
            if owner_id and owner_id in ix.N:
                fqn = ix.N[owner_id].get('fqn', '')
                class_simple = fqn.split('.')[-1] if fqn else ''

            # Тело метода (не более 60 строк)
            body_lines = src_lines[line - 1:min(end_line, line + 60)]
            body = '\n'.join(body_lines)

            # Pre-context для Javadoc (15 строк выше)
            pre_text = '\n'.join(src_lines[max(0, line - 15):line - 1])
            javadoc = _extract_javadoc(pre_text)

            # Logger messages — полный текст первого строкового аргумента
            logger_msgs = _LOGGER_MSG_RE.findall(body)

            # System.out.println
            printlns = _PRINTLN_RE.findall(body)

            # Flow steps (//N. description)
            flow_steps = _FLOW_STEP_RE.findall(body)

            # Line comments в теле
            line_comments = _LINE_COMMENT_RE.findall(body)

            # @PathVariable / @RequestParam переменные
            path_vars = _PATH_VAR_RE.findall(body) + _PATH_VAR_SIMPLE_RE.findall(body)
            path_vars = list(dict.fromkeys(path_vars))  # dedupe, preserve order

            # @RequestBody тип
            req_bodies = [f"{t.strip()} {v}" for t, v in _REQUEST_BODY_RE.findall(body)]

            # Соседние методы в том же классе (контекст: чем занимается класс)
            sibling_nids = by_class.get(owner_id, [])
            sibling_names = [
                ix.N[sid]['name']
                for sid in sibling_nids
                if sid != nid and ix.N.get(sid, {}).get('name')
            ][:8]

            # Сборка CE doc_text (иерархия: сервис → класс → метод → семантика)
            parts: list[str] = []

            # 1. Структура
            parts.append(f"service {module}")
            parts.append(f"class {class_simple}")
            parts.append(f"method {name}")

            # 2. Javadoc
            if javadoc:
                parts.append(javadoc)

            # 3. Logger messages (самый ценный сигнал)
            for msg in logger_msgs[:6]:
                parts.append(msg)

            # 4. Flow steps
            for step in flow_steps[:5]:
                parts.append(step.strip())

            # 5. Line comments
            for comment in line_comments[:3]:
                parts.append(comment.strip())

            # 6. Println
            for p in printlns[:2]:
                parts.append(p)

            # 7. Параметры (@PathVariable имена)
            if path_vars:
                parts.append(' '.join(path_vars))
            for rb in req_bodies[:2]:
                parts.append(rb)

            # 8. Контекст класса (соседние методы)
            if sibling_names:
                parts.append('class context: ' + ' '.join(sibling_names))

            docs[nid] = ' '.join(parts)

    return docs


# Content between [brackets] in logger messages
_LOGGER_BRACKET_RE = re.compile(r'\[([^\[\]]{2,60})\]')


def build_nl_enrichment(ix: "Index", targets_root: "Path | None") -> dict[str, list[str]]:
    """Лёгкое NL-обогащение для BGE-M3: de-bracketed logger content + flow steps.

    Возвращает {nid: [word, ...]} — список NL-слов для добавления в BGE-M3 doc_texts.
    Слова не фильтруются здесь — фильтрация через TF-IDF (is_informative) в searcher.py.
    """
    if targets_root is None:
        return {}

    targets_root = Path(targets_root)

    java_files: dict[str, Path] = {}
    for p in targets_root.rglob('*.java'):
        java_files[p.name] = p

    by_file: dict[str, list[tuple[str, int, int]]] = defaultdict(list)
    for nid, _verb, _tpl in ix.handlers:
        n = ix.N.get(nid, {})
        f = n.get('file', '')
        line = n.get('line')
        end_line = n.get('end')
        if f and line and end_line:
            by_file[Path(f).name].append((nid, line, end_line))

    result: dict[str, list[str]] = {}

    for fname, entries in by_file.items():
        src_path = java_files.get(fname)
        if src_path is None:
            continue
        try:
            src_text = src_path.read_text(errors='ignore')
            src_lines = src_text.splitlines()
        except Exception:
            continue

        for nid, line, end_line in entries:
            body_lines = src_lines[line - 1:min(end_line, line + 60)]
            body = '\n'.join(body_lines)

            words: list[str] = []

            # De-bracketed logger content: [getTransferSearch][Search Transit] → "Search Transit"
            # Skip brackets that look like method/class names (camelCase) or format strings
            for msg in _LOGGER_MSG_RE.findall(body)[:4]:
                for bracket_content in _LOGGER_BRACKET_RE.findall(msg):
                    # Skip if it's a camelCase identifier or contains {} or is too short
                    stripped = bracket_content.strip()
                    if '{' in stripped or '}' in stripped:
                        continue
                    if stripped[0].islower() and any(c.isupper() for c in stripped[1:]):
                        continue  # likely camelCase method name
                    # Split into words and add
                    for w in stripped.lower().split():
                        if len(w) > 2 and w.isalpha():
                            words.append(w)

            # Flow steps: "//1.Check if the order can come in" → ["check", "order", "come"]
            for step_text in _FLOW_STEP_RE.findall(body)[:5]:
                for w in step_text.lower().split():
                    w = w.strip('.,;:')
                    if len(w) > 2 and w.isalpha():
                        words.append(w)

            if words:
                result[nid] = words

    return result
