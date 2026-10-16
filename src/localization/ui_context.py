"""Контекст UI: сопоставление frontend-вызовов API с Java-обработчиками.

Сканирует JS/HTML/Vue-файлы фронтенда → находит URL-литералы → сопоставляет
с @*Mapping шаблонами из скелета → строит документы обработчиков для BM25.

Замена baseline_nlp.ui_context() и baseline_nlp.handler_docs() —
без зависимости от baseline-скриптов.
"""
from __future__ import annotations

import collections
import os
import re
from pathlib import Path

from .._config import cfg_get
from ..core.nlp import ntoks, path_segs


# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------

_FRONT_EXT = (".js", ".html", ".htm", ".vue", ".ts", ".jsx", ".tsx")

_SKIP_DIR: set[str] = {
    "node_modules", "target", "build", "dist", ".git", "bower_components",
}

_VENDOR = re.compile(
    r"(\.min\.|^(vue|angular|jquery|bootstrap|echarts|amazeui|iscroll|"
    r"cropper|aliyun|react|lodash|moment|d3|three)\b)",
    re.I,
)

_PATH_LIT = re.compile(
    r"""["'`](/[A-Za-z0-9_\-./{}$:]*[A-Za-z0-9_}])/?\s*["'`]"""
)

_VERB = re.compile(
    r"""(?:type|method)\s*:\s*["'](get|post|put|delete|patch)["']|"""
    r"""\$http\.(get|post|put|delete|patch)\(|"""
    r"""axios\.(get|post|put|delete|patch)\(|"""
    r"""\.(get|post|put|delete|patch)\(\s*["'`]/""",
    re.I,
)

_FUNC = re.compile(
    r"^\s*(?:async\s+)?(?:function\s+)?([A-Za-z_$][\w$]*)\s*"
    r"(?::\s*(?:async\s+)?function)?\s*\([^)]*\)\s*(?:=>\s*)?\{\s*$|"
    r"^\s*(?:const|let|var|\$scope\.|this\.)?\s*([A-Za-z_$][\w$.]*)\s*=\s*"
    r"(?:async\s*)?(?:function\s*)?\([^)]*\)\s*(?:=>)?\s*\{?\s*$"
)

_KEYWORDS: frozenset[str] = frozenset({
    "if", "for", "while", "switch", "catch", "function", "else", "return",
    "try", "do", "onConfirm", "onCancel", "success", "error", "then",
    "callback", "done", "fail", "always", "complete",
})

_LABEL = re.compile(
    r"""<(\w+)[^>]*?(?:onclick|v-on:click|@click|ng-click|\(click\))\s*="""
    r"""\s*["']\s*([A-Za-z_$][\w$]*)\s*\(.*?>(.*?)</\1>""",
    re.S,
)

_NOISE: frozenset[str] = frozenset("""
    ajax json type url data success error function result status headers
    authorization bearer token session storage content application alert
    console log window location reload var let const this that return true
    false null undefined push length document href value text html attr
    disabled true string object new array get post put delete method
    dataType contentType xhrFields withCredentials responseJSON message
    indexOf setItem getItem stringify parse then catch async await response
    request params query body import export default class extends require
    module exports
""".split())


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _prefix_match(segs: list[str], tpl: list[str]) -> bool:
    """Литерал пути из frontend против Spring-шаблона.

    Литерал может обрываться перед хвостом из переменных.
    """
    for i, s in enumerate(segs):
        if i >= len(tpl):
            return False
        t = tpl[i]
        if t == "**":
            return True
        if not (t.startswith("{") or t == "*" or t == s):
            return False
    return all(t.startswith("{") or t in ("*", "**") for t in tpl[len(segs):])


def _front_files(root: str):
    """Итерация по frontend-файлам: JS/HTML/Vue, кроме vendor и больших файлов."""
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in _SKIP_DIR and not d.startswith(".")]
        for f in fns:
            if f.endswith(_FRONT_EXT) and not _VENDOR.search(f):
                p = os.path.join(dp, f)
                if os.path.getsize(p) < 400_000:
                    yield p


def _html_labels(root: str) -> dict[str, list[str]]:
    """Надписи UI-элементов с click-обработчиками: функция → токены надписи."""
    labels: dict[str, list[str]] = collections.defaultdict(list)
    for p in _front_files(root):
        if not p.endswith((".html", ".htm", ".vue")):
            continue
        try:
            text = open(p, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for m in _LABEL.finditer(text):
            labels[m.group(2)] += ntoks(
                re.sub(r"<[^>]+>|\{\{.*?\}\}", " ", m.group(3))
            )
    return labels


# ---------------------------------------------------------------------------
# Основные функции
# ---------------------------------------------------------------------------

def ui_context(
    root: str, ix
) -> tuple[dict[str, list[str]], dict[str, set[str]], int]:
    """Frontend → документы и файлы для каждого обработчика.

    Сканирует JS/HTML-файлы, находит URL-литералы, сопоставляет с
    @*Mapping шаблонами скелета, собирает контекстные токены:
    имя функции, надпись кнопки, файл, сообщения, поля.

    → (tokens: {handler: [...]}, files: {handler: {file}}, n_sites)
    """
    tokens: dict[str, list[str]] = collections.defaultdict(list)
    files: dict[str, set[str]] = collections.defaultdict(set)
    n = 0
    labels = _html_labels(root)

    for p in _front_files(root):
        try:
            lines = open(p, encoding="utf-8", errors="replace").read().splitlines()
        except OSError:
            continue
        rel = os.path.relpath(p, root)
        fname = ntoks(os.path.basename(p).rsplit(".", 1)[0].replace("-", "_"))

        for i, line in enumerate(lines):
            for m in _PATH_LIT.finditer(line):
                segs = path_segs(m.group(1))
                if len(segs) < 2:
                    continue
                concat = bool(re.match(r"\s*\+", line[m.end():]))
                verb = None
                for j in list(range(i, max(-1, i - 10), -1)) + list(
                    range(i + 1, min(len(lines), i + 10))
                ):
                    v = _VERB.search(lines[j])
                    if v:
                        verb = next(g for g in v.groups() if g).upper()
                        break

                hs: set[str] = set()
                for nid, hv, tpl in ix.handlers:
                    if verb is not None and hv is not None and hv != verb:
                        continue
                    if concat and _prefix_match(segs, tpl) or (
                        not concat
                        and len(segs) == len(tpl)
                        and _prefix_match(segs, tpl)
                    ):
                        hs.add(nid)

                if not hs:
                    continue

                start, func = max(0, i - 60), None
                for j in range(i, start - 1, -1):
                    fm = _FUNC.match(lines[j])
                    if fm and (fm.group(1) or fm.group(2)).split(".")[-1] not in _KEYWORDS:
                        func = (fm.group(1) or fm.group(2)).split(".")[-1]
                        start = j
                        break

                ctx = " ".join(lines[start: min(len(lines), i + 15)])
                words = sorted(
                    {
                        w
                        for w in ntoks(re.sub(r"https?://\S+", " ", ctx))
                        if w not in _NOISE
                    }
                )
                n += 1
                for h in hs:
                    tokens[h] += (
                        fname
                        + ntoks(func or "") * 2
                        + labels.get(func, [])
                        + words
                    )
                    files[h].add(rel)

    return tokens, files, n


def handler_docs(
    ix, ui_tokens: dict[str, list[str]]
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Построить документы обработчиков для BM25.

    Два вида документов:
      code — имя×2, класс, модуль, путь×2, типы параметров, поля DTO
      ui   — контекстные токены из frontend (из ui_context())

    → (code_docs, ui_docs)
    """
    N = ix.N
    code: dict[str, list[str]] = {}
    ui: dict[str, list[str]] = {}
    fields: dict[str, list[str]] = collections.defaultdict(list)

    for nid, n in N.items():
        if n["kind"] == "field":
            fields[n["owner"]].append(n["name"])

    by_simple: dict[str, list[str]] = collections.defaultdict(list)
    for t, x in N.items():
        if x["kind"] == "type":
            by_simple[x["fqn"].split(".")[-1]].append(t)

    for nid, verb, tpl in ix.handlers:
        n = N[nid]
        if nid not in code:
            d = (
                ntoks(n["name"]) * 2
                + ntoks(N[n["owner"]]["fqn"].split(".")[-1])
                + ntoks(n["module"])
            )
            for pt in n.get("params_as_written") or []:
                d += ntoks(pt)
                for t in by_simple.get(pt.split("<")[0], []):
                    d += ntoks(" ".join(fields.get(t, [])))
            code[nid] = d
            if ui_tokens.get(nid):
                ui[nid] = list(ui_tokens[nid])
        code[nid] = code[nid] + ntoks(" ".join(tpl)) * 2

    return code, ui
