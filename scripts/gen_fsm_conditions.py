#!/usr/bin/env python3
"""Аннотация FSM transitions условиями через AST-анализ Java кода + Qwen.

Алгоритм для каждого transition:
  1. Найти Java-файл via_method через from_nid (модуль → targets/ дерево)
  2. Извлечь тело метода via_method из Java-файла (балансировка скобок)
  3. Qwen анализирует тело: является ли вызов downstream условным?
  4. Записать condition {is_conditional, condition_text, condition_keywords}

Результат сохраняется обратно в cache/fsm_transitions.json.
Уже аннотированные transitions пропускаются (condition != null).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

TARGETS = ROOT / "tt-challenge" / "targets" / "trainticket"
CACHE_PATH = ROOT / "cache" / "fsm_transitions.json"


# ---------------------------------------------------------------------------
# Java AST: извлечение тела метода из файла
# ---------------------------------------------------------------------------

def _find_java_file(module: str, class_hint: str | None = None) -> Path | None:
    """Найти Java-файл для данного модуля + имени класса."""
    svc_dir = TARGETS / module
    if not svc_dir.exists():
        return None
    for pattern in (
        f"**/{class_hint}.java" if class_hint else None,
        "**/*ServiceImpl.java",
        "**/*.java",
    ):
        if pattern is None:
            continue
        files = list(svc_dir.glob(pattern))
        if files:
            return files[0]
    return None


def _find_file_with_method_call(module: str, entry_method: str, via_method: str) -> Path | None:
    """Найти Java-файл в модуле, где entry_method вызывает via_method.

    Контроллер делегирует в ServiceImpl, поэтому if-условия вокруг via-вызовов
    находятся в ServiceImpl, а не в Controller. Ищем файл с обоими методами.
    """
    svc_dir = TARGETS / module
    if not svc_dir.exists():
        return None
    # Сначала ServiceImpl (самый вероятный), потом остальные
    candidates = (
        list(svc_dir.glob("**/*ServiceImpl.java"))
        + [f for f in svc_dir.glob("**/*.java") if "ServiceImpl" not in f.name]
    )
    for java_file in candidates:
        try:
            src = java_file.read_text(errors="replace")
        except OSError:
            continue
        # Проверить: есть ли entry_method И via_method в этом файле
        has_entry = re.search(
            r'(?:private|public|protected)[^{]*\s' + re.escape(entry_method) + r'\s*\(',
            src,
        )
        has_via_call = via_method + "(" in src
        if has_entry and has_via_call:
            return java_file
    return None


def _class_from_nid(nid: str) -> str | None:
    """Извлечь простое имя класса из nid.

    nid: 'method:ts-preserve-service:preserve.service.PreserveServiceImpl#preserve(...)'
    → 'PreserveServiceImpl'
    """
    m = re.search(r"[#(]", nid)
    if not m:
        return None
    fqn_part = nid[:m.start()]          # 'method:mod:fqn.ClassName'
    simple = fqn_part.rsplit(".", 1)[-1]
    simple = simple.rsplit(":", 1)[-1]  # убрать 'method:mod:' если нет точек
    return simple if simple else None


def _method_name_from_nid(nid: str) -> str | None:
    """Извлечь имя метода из nid.

    nid: 'method:ts-preserve-service:...PreserveServiceImpl#preserve(...)'
    → 'preserve'
    """
    m = re.search(r"#([^(]+)\(", nid)
    return m.group(1) if m else None


def extract_call_site_context(entry_body: str, via_method: str, context_lines: int = 20) -> str | None:
    """Извлечь контекст вызова via_method внутри entry body.

    Алгоритм с отслеживанием скобок: идём назад от вызова, ищем ближайший
    if-блок, непосредственно содержащий вызов (через баланс {}).
    Fallback: контекстное окно вокруг вызова.
    """
    idx = entry_body.find(via_method + "(")
    if idx == -1:
        return None

    # Алгоритм: идти назад, отслеживать { } и найти if-блок вокруг вызова
    depth = 0
    i = idx - 1
    enclosing_if_pos = None
    while i >= 0:
        c = entry_body[i]
        if c == "}":
            depth += 1
        elif c == "{":
            if depth == 0:
                # Нашли открывающую { для блока, который содержит наш вызов
                # Проверить наличие 'if (' перед ней
                pre_brace = entry_body[max(0, i - 300): i]
                m = re.search(r"if\s*\(", pre_brace)
                if m:
                    enclosing_if_pos = max(0, i - 300) + m.start()
                    break  # Нашли ближайший if-блок
                # Нет if — это просто вложенный блок (try, else, etc.), продолжаем
            else:
                depth -= 1
        i -= 1

    if enclosing_if_pos is not None:
        # Вернуть от if до 300 символов после вызова
        snippet = entry_body[enclosing_if_pos: min(len(entry_body), idx + 300)]
    else:
        # Fallback: фиксированное окно вокруг вызова
        start = max(0, idx - 800)
        end   = min(len(entry_body), idx + 300)
        snippet = entry_body[start:end]

    lines = snippet.splitlines()
    if len(lines) > context_lines * 2:
        call_line = None
        for j, ln in enumerate(lines):
            if via_method + "(" in ln:
                call_line = j
                break
        if call_line is not None:
            lines = lines[max(0, call_line - context_lines): call_line + 5]
    return "\n".join(lines)


def extract_method_body(java_src: str, method_name: str) -> str | None:
    """Извлечь тело метода из Java-источника (балансировка фигурных скобок).

    Возвращает всё тело метода включая объявление, или None если не найден.
    Ограничение: берёт первый найденный метод с таким именем.
    """
    # Найти сигнатуру метода: `... method_name(` на отдельной (или смешанной) строке
    pattern = re.compile(
        r'(?:private|public|protected|static|\s)+\w[\w<>\[\], ]*\s+'
        + re.escape(method_name)
        + r'\s*\(',
        re.MULTILINE,
    )
    m = pattern.search(java_src)
    if not m:
        return None

    # Найти открывающую { после сигнатуры (может быть throws ...)
    start = m.start()
    brace_pos = java_src.find("{", m.end())
    if brace_pos == -1:
        return None

    # Балансировать скобки
    depth = 0
    i = brace_pos
    while i < len(java_src):
        if java_src[i] == "{":
            depth += 1
        elif java_src[i] == "}":
            depth -= 1
            if depth == 0:
                return java_src[start : i + 1]
        i += 1
    return None  # незакрытый блок (маловероятно)


# ---------------------------------------------------------------------------
# Qwen: аннотация условий
# ---------------------------------------------------------------------------

CONDITION_PROMPT = """\
Look at this Java code snippet from an entry method that calls `{via_method}`:

```java
{call_context}
```

Is the call to `{via_method}(...)` inside an `if` block that checks request/order data?

Respond with JSON:
{{
  "is_conditional": true or false,
  "condition_text": "business description, e.g. order includes food option",
  "condition_keywords": ["keyword1", "keyword2"]
}}

Rules:
- condition_text: BUSINESS meaning, not Java field. "order includes food" NOT "foodType != 0"
- condition_keywords: 2-4 words user says when this option IS present
- If is_conditional=false: condition_text="" and condition_keywords=[]
"""

_CONDITION_SCHEMA = {
    "type": "object",
    "properties": {
        "is_conditional":     {"type": "boolean"},
        "condition_text":     {"type": "string"},
        "condition_keywords": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["is_conditional", "condition_text", "condition_keywords"],
}


def annotate_via_qwen(
    qwen,
    via_method: str,
    to_module: str,
    to_handler: str,
    call_context: str,
) -> dict | None:
    """Вызвать Qwen для определения условия transition."""
    prompt = CONDITION_PROMPT.format(
        via_method=via_method,
        call_context=call_context[:2000],
    )
    try:
        return qwen._call_ollama(prompt, schema=_CONDITION_SCHEMA, num_predict=200)
    except Exception as e:
        print(f"  [qwen] ошибка: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Основной цикл
# ---------------------------------------------------------------------------

def main() -> None:
    if not CACHE_PATH.exists():
        sys.exit(f"Нет {CACHE_PATH}. Запусти сначала: python scripts/gen_fsm_transitions.py")

    with open(CACHE_PATH) as f:
        data = json.load(f)

    forward: dict[str, list[dict]] = data["forward"]

    # Инициализировать Qwen
    from src.localization.qwen_encoder import QwenEncoder
    qwen = QwenEncoder(targets_root=ROOT / "tt-challenge" / "targets")
    if not qwen.enabled:
        sys.exit("Qwen недоступен (ollama не запущен?). Запусти: ollama serve")

    # Кеш уже обработанных (via_method_body) чтобы не дублировать Qwen-вызовы
    # Ключ: (module, via_method) → condition dict
    via_cache: dict[tuple[str, str], dict | None] = {}
    updated = 0
    skipped = 0
    errors = 0

    for entry_nid, transitions in forward.items():
        entry_mod = entry_nid.split(":")[1] if ":" in entry_nid else ""
        class_name = _class_from_nid(entry_nid)
        entry_method = _method_name_from_nid(entry_nid)

        for t in transitions:
            # Пропустить уже аннотированные
            if t.get("condition") is not None:
                skipped += 1
                continue

            via = t.get("via")
            if via is None:
                # Прямой remote_call от entry — всегда безусловный (entry сам вызывает)
                t["condition"] = {
                    "is_conditional": False,
                    "condition_text": "",
                    "condition_keywords": [],
                }
                updated += 1
                continue

            # cache_key по (entry_nid, via) — один entry может вызывать via по-разному
            cache_key = (entry_nid, via)
            if cache_key in via_cache:
                t["condition"] = via_cache[cache_key]
                if t["condition"] is not None:
                    updated += 1
                continue

            if not entry_method:
                print(f"  [warn] Не удалось извлечь имя entry-метода: {entry_nid}", file=sys.stderr)
                via_cache[cache_key] = None
                errors += 1
                continue

            # Найти Java файл где entry_method содержит вызов via
            # (Логика с условиями обычно в ServiceImpl, а не Controller)
            java_file = _find_file_with_method_call(entry_mod, entry_method, via)
            if java_file is None:
                # Fallback: взять первый доступный файл
                java_file = _find_java_file(entry_mod, class_name) or _find_java_file(entry_mod)

            if java_file is None:
                print(f"  [warn] Java файл не найден: {entry_mod}", file=sys.stderr)
                via_cache[cache_key] = None
                errors += 1
                continue

            src = java_file.read_text(errors="replace")

            entry_body = extract_method_body(src, entry_method)
            if not entry_body:
                print(f"  [warn] Entry-метод {entry_method} не найден в {java_file.name}", file=sys.stderr)
                via_cache[cache_key] = None
                errors += 1
                continue

            # Извлечь контекст вызова via внутри entry-метода
            call_context = extract_call_site_context(entry_body, via)
            if not call_context:
                # via не найден напрямую в entry body → indirect call → безусловный
                print(f"  [info] {via}() не в {entry_method}() напрямую → unconditional", file=sys.stderr)
                unconditional = {"is_conditional": False, "condition_text": "", "condition_keywords": []}
                via_cache[cache_key] = unconditional
                t["condition"] = unconditional
                updated += 1
                continue

            print(
                f"  [{entry_mod}::{entry_method}] calls {via}() "
                f"(context={len(call_context)} chars)",
                flush=True,
            )

            condition = annotate_via_qwen(
                qwen,
                via_method=via,
                to_module=t["to_module"],
                to_handler=t["to_handler"],
                call_context=call_context,
            )

            if condition is None:
                print(f"    [warn] Qwen не вернул условие", file=sys.stderr)
                via_cache[cache_key] = None
                errors += 1
                continue

            is_cond = condition.get("is_conditional", False)
            keywords = condition.get("condition_keywords", [])
            print(
                f"    → is_conditional={is_cond}, keywords={keywords}",
                flush=True,
            )

            via_cache[cache_key] = condition
            t["condition"] = condition
            updated += 1

    # Сохранить
    data["forward"] = forward
    with open(CACHE_PATH, "w") as f:
        json.dump(data, f, indent=2)

    print(f"\nГотово: обновлено={updated}, пропущено={skipped}, ошибок={errors}")
    print(f"Сохранено: {CACHE_PATH}")


if __name__ == "__main__":
    main()
