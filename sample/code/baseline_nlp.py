#!/usr/bin/env python3
"""Бейзлайн «NLP + статика» для входа «только текст»: подписи шагов сценария сопоставляются с обработчиками по двум
источникам текста — самому коду обработчика и фронтенду проекта, — дальше статическое замыкание (baseline_closure.Index).

Идея: бизнес-словарь сценария — это словарь пользовательского интерфейса. В репозитории есть фронтенд (JS/HTML), который
вызывает API: каждое место вызова (`url: "/api/v1/executeservice/execute/collected/" + orderId`) окружено текстом —
имя функции (`collectTicket`), надпись кнопки, имя файла (`client_collect.js`), сообщения («you can enter station with
your order id»), имена полей. Путь и глагол вызова сопоставляются с шаблонами @RequestMapping, и этот текст становится
«документом UI» обработчика. Второй документ — «документ кода»: имя метода, класс, модуль, путь, типы параметров и
поля DTO. Подпись шага (без отрицаемых слов: «no insurance», «not high-speed») ищется BM25 в обоих индексах, баллы
нормируются по лучшему и складываются; обработчики с почти равным баллом (travel/travel2, order/orderOther) берутся все.
Обработчики административных страниц штрафуются, если в сценарии нет администратора. Глаголы шагов расширяются
общими CRUD-синонимами (open/view/check → get/find/query…). От найденных обработчиков — замыкание по calls/CHA/
проводке, все операторы найденных методов.

    python3 baseline_nlp.py <skeleton.json[.gz]> <корень исходников> <scenario.json> -o submission.json [--explain] [--gold gold.json]"""
import os, re, sys, json, argparse, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from baseline_closure import Index, BM25, toks, path_segs, load_json

FRONT_EXT = (".js", ".html", ".htm", ".vue", ".ts", ".jsx", ".tsx")
SKIP_DIR = {"node_modules", "target", "build", "dist", ".git", "bower_components"}
VENDOR = re.compile(r"(\.min\.|^(vue|angular|jquery|bootstrap|echarts|amazeui|iscroll|cropper|aliyun|react|lodash|moment|d3|three)\b)", re.I)
PATH_LIT = re.compile(r"""["'`](/[A-Za-z0-9_\-./{}$:]*[A-Za-z0-9_}])/?\s*["'`]""")
VERB = re.compile(r"""(?:type|method)\s*:\s*["'](get|post|put|delete|patch)["']|\$http\.(get|post|put|delete|patch)\(|axios\.(get|post|put|delete|patch)\(|\.(get|post|put|delete|patch)\(\s*["'`]/""", re.I)
FUNC = re.compile(r"^\s*(?:async\s+)?(?:function\s+)?([A-Za-z_$][\w$]*)\s*(?::\s*(?:async\s+)?function)?\s*\([^)]*\)\s*(?:=>\s*)?\{\s*$|"
                  r"^\s*(?:const|let|var|\$scope\.|this\.)?\s*([A-Za-z_$][\w$.]*)\s*=\s*(?:async\s*)?(?:function\s*)?\([^)]*\)\s*(?:=>)?\s*\{?\s*$")
KEYWORDS = {"if", "for", "while", "switch", "catch", "function", "else", "return", "try", "do",
            "onConfirm", "onCancel", "success", "error", "then", "callback", "done", "fail", "always", "complete"}
LABEL = re.compile(r"""<(\w+)[^>]*?(?:onclick|v-on:click|@click|ng-click|\(click\))\s*=\s*["']\s*([A-Za-z_$][\w$]*)\s*\(.*?>(.*?)</\1>""", re.S)
VOWELS = set("aeiouy")


def stem(w):
    """Консервативный стеммер: trains -> train, stores -> store, details -> detail, departing -> depart, cancelled -> cancel,
    booked -> book; не трогает короткие основы (speed, paid) и не режет -ers/-ment/-ure (orders -> order, payment)."""
    if len(w) >= 5 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) >= 5 and w.endswith("es") and w[-3] in "sxz" or w.endswith(("ches", "shes")):
        w = w[:-2]
    elif len(w) >= 4 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        w = w[:-1]
    for suf in ("ing", "ed"):
        if w.endswith(suf) and len(w) - len(suf) >= 4 and any(c in VOWELS for c in w[:-len(suf)]):
            w = w[:-len(suf)]
            if len(w) > 4 and w[-1] == w[-2] and w[-1] not in VOWELS:
                w = w[:-1]
            break
    return w


def ntoks(t):
    return [stem(w) for w in toks(t)]


NEG = re.compile(r"\b(?:no|not|without|non)[- ]+([a-z][a-z-]*(?:\s+[a-z][a-z-]*){0,2}?)(?=\s*(?:,|;|\band\b|\bor\b|\)|$))", re.I)


def query_toks(text):
    """Токены подписи шага без отрицаемых слов («no insurance», «non-high-speed», «without luggage»)."""
    return ntoks(NEG.sub(" ", text))
NOISE = set("""ajax json type url data success error function result status headers authorization bearer token session storage content
application alert console log window location reload var let const this that return true false null undefined push length
document href value text html attr disabled true string object new array get post put delete method dataType contentType
xhrFields withCredentials responseJSON message indexOf setItem getItem stringify parse then catch async await response
request params query body import export default class extends require module exports """.split())


def prefix_match(segs, tpl):
    """Литерал пути из фронтенда против шаблона обработчика: литерал может обрываться перед хвостом из переменных
    (`"/execute/collected/" + orderId`)."""
    i = 0
    for i, s in enumerate(segs):
        if i >= len(tpl):
            return False
        t = tpl[i]
        if t == "**":
            return True
        if not (t.startswith("{") or t == "*" or t == s):
            return False
    return all(t.startswith("{") or t in ("*", "**") for t in tpl[len(segs):])


def front_files(root):
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in SKIP_DIR and not d.startswith(".")]
        for f in fns:
            if f.endswith(FRONT_EXT) and not VENDOR.search(f):
                p = os.path.join(dp, f)
                if os.path.getsize(p) < 400_000:
                    yield p


def html_labels(root):
    """Надписи элементов интерфейса, вызывающих JS-функцию (onclick / v-on:click / ng-click): имя функции -> токены надписи."""
    labels = collections.defaultdict(list)
    for p in front_files(root):
        if not p.endswith((".html", ".htm", ".vue")):
            continue
        try:
            text = open(p, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for m in LABEL.finditer(text):
            labels[m.group(2)] += ntoks(re.sub(r"<[^>]+>|\{\{.*?\}\}", " ", m.group(3)))
    return labels


def ui_context(root, ix):
    """Фронтенд -> для каждого обработчика: токены окружения мест вызова его API (имя функции, надпись кнопки, файл,
    сообщения, поля) и файлы, где он вызывается. -> (tokens: {handler: [..]}, files: {handler: {file}}, n мест вызова)"""
    tokens, files, n = collections.defaultdict(list), collections.defaultdict(set), 0
    labels = html_labels(root)
    for p in front_files(root):
        try:
            lines = open(p, encoding="utf-8", errors="replace").read().splitlines()
        except OSError:
            continue
        rel = os.path.relpath(p, root); fname = ntoks(os.path.basename(p).rsplit(".", 1)[0].replace("-", "_"))
        for i, line in enumerate(lines):
            for m in PATH_LIT.finditer(line):
                segs = path_segs(m.group(1))
                if len(segs) < 2:
                    continue
                concat = bool(re.match(r"\s*\+", line[m.end():]))
                verb = None
                for j in list(range(i, max(-1, i - 10), -1)) + list(range(i + 1, min(len(lines), i + 10))):
                    v = VERB.search(lines[j])
                    if v:
                        verb = next(g for g in v.groups() if g).upper(); break
                hs = set()
                for nid, hv, tpl in ix.handlers:
                    if verb is not None and hv is not None and hv != verb:
                        continue
                    if concat and prefix_match(segs, tpl) or not concat and len(segs) == len(tpl) and prefix_match(segs, tpl):
                        hs.add(nid)
                if not hs:
                    continue
                start, func = max(0, i - 60), None
                for j in range(i, start - 1, -1):
                    fm = FUNC.match(lines[j])
                    if fm and (fm.group(1) or fm.group(2)).split(".")[-1] not in KEYWORDS:
                        func = (fm.group(1) or fm.group(2)).split(".")[-1]; start = j; break
                ctx = " ".join(lines[start:min(len(lines), i + 15)])
                words = sorted({w for w in ntoks(re.sub(r"https?://\S+", " ", ctx)) if w not in NOISE})
                n += 1
                for h in hs:
                    tokens[h] += fname + ntoks(func or "") * 2 + labels.get(func, []) + words; files[h].add(rel)
    return tokens, files, n


def handler_docs(ix, ui_tokens):
    """Два документа обработчика: код (имя x2, класс, модуль, путь x2, типы параметров, поля DTO) и UI (окружение вызовов)."""
    N = ix.N; code, ui = {}, {}
    fields = collections.defaultdict(list)
    for nid, n in N.items():
        if n["kind"] == "field":
            fields[n["owner"]].append(n["name"])
    by_simple = collections.defaultdict(list)
    for t, x in N.items():
        if x["kind"] == "type":
            by_simple[x["fqn"].split(".")[-1]].append(t)
    for nid, verb, tpl in ix.handlers:
        n = N[nid]
        if nid not in code:
            d = ntoks(n["name"]) * 2 + ntoks(N[n["owner"]]["fqn"].split(".")[-1]) + ntoks(n["module"])
            for pt in n.get("params_as_written") or []:
                d += ntoks(pt)
                for t in by_simple.get(pt.split("<")[0], []):
                    d += ntoks(" ".join(fields.get(t, [])))
            code[nid] = d
            if ui_tokens.get(nid):
                ui[nid] = list(ui_tokens[nid])
        code[nid] = code[nid] + ntoks(" ".join(tpl)) * 2
    return code, ui


class Scorer:
    """Нормированная сумма BM25 по документу кода и документу UI (каждый — доля от лучшего балла по запросу)."""

    def __init__(self, code, ui):
        self.bm_code, self.bm_ui = BM25(code), BM25(ui)

    def rank(self, q):
        parts = []
        for bm in (self.bm_code, self.bm_ui):
            r = bm.rank(q); m = r[0][0] if r else 1.0
            parts.append({h: sc / m for sc, h in r})
        sc = {h: parts[0].get(h, 0.0) + parts[1].get(h, 0.0) for h in set(parts[0]) | set(parts[1])}
        return sorted(((v, h) for h, v in sc.items()), reverse=True)


READ_VERBS = {"view", "look", "open", "check", "search", "browse", "ask", "see", "list", "show", "read", "find", "get", "query", "display"}
VERB_SYN = {**{v: ["get", "find", "query"] for v in READ_VERBS},
            **{v: ["create", "add", "new"] for v in ("book", "reserve", "create", "add", "register", "place", "submit", "make")},
            **{v: ["update", "modify", "edit"] for v in ("change", "edit", "modify", "update", "set", "rename")},
            **{v: ["delete", "remove", "cancel"] for v in ("cancel", "delete", "remove", "withdraw")}}
ADMIN = {"admin", "administrator", "manager", "operator", "staff", "management"}


def expand(q):
    """Первый глагол подписи шага -> CRUD-синонимы (вес 1)."""
    return q + (VERB_SYN.get(q[0], []) if q else [])


def ground(ix, scorer, files, S, beta=0.0, delta=0.0, tau=0.05, admin_pen=0.5, explain=False):
    """Подпись каждого шага -> обработчики: лучший по нормированной сумме BM25 (код + UI) и все в пределах tau от него
    (travel/travel2, order/orderOther — почти одинаковые документы). Штраф admin_pen обработчикам административных страниц/
    модулей, если в тексте сценария нет администратора. Бонусы beta (та же страница фронтенда, что у предыдущего шага)
    и delta (сервис уже затронут предыдущими шагами) по умолчанию выключены."""
    text = " ".join([S.get("title", ""), S.get("description", "")] + [st.get("action", "") for st in S.get("steps") or []])
    is_admin = bool(ADMIN & set(toks(text)))
    entries, prev_files, touched, log = set(), set(), set(), []
    for st in S.get("steps") or []:
        act = st.get("action") or ""; q = expand(query_toks(act))
        ranked = []
        for sc, h in scorer.rank(q):
            if not is_admin and ("admin" in ix.module[h] or any("admin" in os.path.basename(f) for f in files.get(h, ()))):
                sc *= admin_pen
            if files.get(h, set()) & prev_files:
                sc *= 1 + beta
            if ix.module[h] in touched:
                sc *= 1 + delta
            ranked.append((sc, h))
        ranked.sort(reverse=True)
        hs = set()
        if ranked:
            best = ranked[0][0]
            hs = {h for sc, h in ranked if sc >= best * (1 - tau)}
        entries |= hs; prev_files = set().union(*(files.get(h, set()) for h in hs)) if hs else prev_files
        if delta:
            touched |= {ix.module[m] for m in ix.closure(hs)[0]}
        log.append((st.get("n"), act, [(sc, h) for sc, h in ranked[:4]], hs))
    if explain:
        for n, act, top, hs in log:
            print(f"  {n:2d}. {act[:76]}")
            for sc, h in top:
                print(f"        {'->' if h in hs else '  '} {sc:.2f} {h.split(':', 2)[2].split('(')[0]}")
    return entries


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("skeleton"); ap.add_argument("root", help="корень исходников проекта (targets/trainticket)"); ap.add_argument("scenario", nargs="+", help="файлы сценариев scenarios/*.json — один ответ на все"); ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--tau", type=float, default=0.05, help="допуск равенства баллов: все обработчики в пределах tau от лучшего")
    ap.add_argument("--admin-pen", type=float, default=0.5, help="множитель для административных обработчиков в сценарии без администратора")
    ap.add_argument("--beta", type=float, default=0.0, help="бонус за ту же страницу фронтенда, что у предыдущего шага")
    ap.add_argument("--delta", type=float, default=0.0, help="бонус обработчикам сервисов, уже затронутых предыдущими шагами")
    ap.add_argument("--gold", help="эталон (только диагностика: сколько входов найдено верно)")
    ap.add_argument("--explain", action="store_true"); a = ap.parse_args()
    sk = load_json(a.skeleton); ix = Index(sk)
    gold = json.load(open(a.gold, encoding="utf-8")) if a.gold else None
    ui_tokens, files, n_sites = ui_context(a.root, ix)
    code, ui = handler_docs(ix, ui_tokens); scorer = Scorer(code, ui)
    print(f"мест вызова API во фронтенде {n_sites} (обработчиков с контекстом {len(ui)})")
    out = {"project": sk["project"], "scenarios": {}}
    for path in a.scenario:
        S = load_json(path); sid = S["id"]
        entries = ground(ix, scorer, files, S, a.beta, a.delta, a.tau, a.admin_pen, a.explain)
        if gold and sid in gold["scenarios"]:
            G = set(gold["scenarios"][sid].get("entry") or []); tp = len(entries & G)
            print(f"  входы: верных {tp} из {len(G)} эталонных, лишних {len(entries - G)}")
        methods, edges, remote, data = ix.closure(entries)
        out["scenarios"][sid] = ix.submission(sid, methods, edges, remote, data, method="nlp+closure")
        print(f"{sid}: входов {len(entries)}, методов {len(methods)}, calls {len(edges)}, remote {len(remote)}, data {len(data)}")
    json.dump(out, open(a.out, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"сценариев {len(out['scenarios'])} -> {a.out}")


if __name__ == "__main__":
    main()
