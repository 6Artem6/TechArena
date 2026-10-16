"""Индекс схожести файлов — поиск клонов и похожих файлов по 4 уровням.

Уровни (в порядке убывания точности):
  1. hash       — SHA-256: побайтовые клоны, O(1)
  2. ast_exact  — отпечаток нормализованного скелета: структурные клоны, O(1)
  3. ast_vector — n-граммы скелета: приближённое структурное сходство, cosine O(n)
  4. ctx_vector — n-граммы контекста (импорты+URL+аннотации): семантика, cosine O(n)

Обновление: index_file(path) перестраивает только записи этого файла.
Остальные файлы индекса не затрагиваются — «влияет только на него».

Пример использования:
    idx = SimilarityIndex()
    for path in repo.rglob("*.java"):
        idx.index_file(path)

    similar = idx.query("ts-travel-service/…/TravelServiceImpl.java")
    # → [SimilarFile("ts-seat-service/…/SeatServiceImpl.java", 0.87, "ast_vector"), …]
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from ..utils.text import truncate_code


def _knee_cutoff(results: list["SimilarFile"], drop_ratio: float = 0.15) -> list["SimilarFile"]:
    """Обрезать хвост результатов там, где оценки резко падают.

    При перепаде между соседними оценками >= drop_ratio × best_score
    всё что правее — отбрасывается. Динамичнее фиксированного порога.

    Пример:
        scores = [0.95, 0.92, 0.91, 0.90, 0.60, 0.58]
        drop 0.90→0.60 = 0.30, best=0.95, ratio=0.15, threshold=0.143
        0.30 > 0.143 → обрезаем с индекса 4: возвращаем [0.95, 0.92, 0.91, 0.90]
    """
    if len(results) < 2:
        return results
    best = results[0].similarity
    threshold = drop_ratio * best
    for i in range(1, len(results)):
        if results[i - 1].similarity - results[i].similarity >= threshold:
            return results[:i]
    return results


def _mmr_select(
    candidates: list["SimilarFile"],
    vecs: dict[str, "Counter"],
    lam: float = 0.65,
    k: int = 10,
) -> list["SimilarFile"]:
    """MMR-ранжирование для разнообразия результатов.

    MMR(d) = λ·relevance(d, query) − (1−λ)·max_sim(d, selected)

    Без MMR: 29 SecurityConfig.java в топ-29.
    С MMR (λ=0.65): 1 SecurityConfig + разнообразные структурно близкие файлы.

    relevance берётся из SimilarFile.similarity (уже вычислена).
    max_sim считается через AST-векторы (ast_vecs).
    Файлы без AST-вектора в индексе включаются с max_sim=0.
    """
    selected: list["SimilarFile"] = []
    remaining = list(candidates)

    while remaining and len(selected) < k:
        best_score = float("-inf")
        best_item: "SimilarFile | None" = None

        for item in remaining:
            rel = item.similarity
            if selected:
                iv = vecs.get(item.path)
                max_sim = max(
                    (_cosine(iv, vecs[s.path]) if iv and s.path in vecs else 0.0)
                    for s in selected
                )
            else:
                max_sim = 0.0
            score = lam * rel - (1.0 - lam) * max_sim
            if score > best_score:
                best_score, best_item = score, item

        if best_item is not None:
            selected.append(best_item)
            remaining.remove(best_item)

    return selected


class SimilarityLevel(str, Enum):
    HASH       = "hash"        # побайтовый клон
    AST_EXACT  = "ast_exact"   # структурный клон (одинаковый нормализованный скелет)
    BODY_EXACT = "body_exact"  # совпадает тело без комментариев и импортов
    AST_VECTOR = "ast_vector"  # приближённое структурное сходство по n-граммам
    CTX_VECTOR = "ctx_vector"  # семантика файла (импорты, URL, аннотации)


@dataclass(frozen=True, order=False)
class SimilarFile:
    path: str
    similarity: float       # 0.0–1.0
    level: SimilarityLevel

    def __lt__(self, other: "SimilarFile") -> bool:
        return self.similarity > other.similarity  # сортировка по убыванию


class SimilarityIndex:
    """Индекс схожести файлов. In-memory, single-process.

    Инвариант: каждый файл хранит ровно один набор записей.
    При вызове index_file(A, new_content) — удаляются только записи A,
    добавляются новые для A. Остальные файлы (B, C, …) не изменяются.
    """

    def __init__(
        self,
        ast_threshold: float = 0.70,
        ctx_threshold: float = 0.55,
    ) -> None:
        self.ast_threshold = ast_threshold
        self.ctx_threshold = ctx_threshold

        # Инвертированные индексы: значение → множество путей
        self._hash_index: dict[str, set[str]] = {}    # sha256    → {path, …}
        self._fp_index:   dict[str, set[str]] = {}    # ast_fp    → {path, …}
        self._body_index: dict[str, set[str]] = {}    # body_fp   → {path, …}

        # Векторы per-file
        self._ast_vecs: dict[str, Counter] = {}   # path → n-грамм вектор скелета
        self._ctx_vecs: dict[str, Counter] = {}   # path → n-грамм вектор контекста

        # Обратный lookup: path → текущий ключ (для удаления из индекса)
        self._file_hashes:    dict[str, str] = {}  # path → sha256
        self._file_fps:       dict[str, str] = {}  # path → ast_fp
        self._file_body_fps:  dict[str, str] = {}  # path → body_fp

    # ─── Публичный API ────────────────────────────────────────────────────────

    def index_file(
        self,
        path: Path | str,
        content: str | None = None,
        context: str | None = None,
    ) -> None:
        """Добавить / обновить файл в индексе.

        При повторном вызове с тем же path — старые записи удаляются,
        пересчитываются только для этого файла.

        Args:
            path:    путь к файлу (используется как ключ)
            content: текст файла; если None — читается с диска
            context: дополнительный текст контекста (импорты, вызовы из AnalysisResult);
                     если None — извлекается из content автоматически
        """
        key = str(path)
        if content is None:
            try:
                content = Path(path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                return

        # Чистим старые записи этого файла перед переиндексацией
        self._evict(key)

        # Уровень 1: SHA-256 контента
        sha = _sha256(content)
        self._file_hashes[key] = sha
        self._hash_index.setdefault(sha, set()).add(key)

        # Уровень 2: отпечаток нормализованного скелета
        fp = _ast_fingerprint(content, str(path))
        self._file_fps[key] = fp
        self._fp_index.setdefault(fp, set()).add(key)

        # Уровень 2.5: тело без комментариев и импортов
        bfp = _body_fingerprint(content, str(path))
        self._file_body_fps[key] = bfp
        self._body_index.setdefault(bfp, set()).add(key)

        # Уровень 3: n-грамм вектор скелета
        self._ast_vecs[key] = _ngrams(_ast_skeleton(content, str(path)))

        # Уровень 4: n-грамм вектор контекста
        ctx_text = context if context is not None else _context_text(content, str(path))
        self._ctx_vecs[key] = _ngrams(ctx_text)

    def remove_file(self, path: Path | str) -> None:
        """Удалить файл из всех индексов."""
        self._evict(str(path))

    def query(
        self,
        path: Path | str,
        content: str | None = None,
        context: str | None = None,
        top_k: int = 10,
        use_mmr: bool = True,
        mmr_lambda: float = 0.65,
        use_knee: bool = True,
        knee_drop_ratio: float = 0.15,
    ) -> list[SimilarFile]:
        """Найти файлы, похожие на `path`. Не изменяет индекс.

        Args:
            use_mmr:         MMR-ранжирование для разнообразия результатов.
                             Предотвращает возврат 29 идентичных SecurityConfig.java.
            mmr_lambda:      0.0 = только разнообразие, 1.0 = только релевантность.
            use_knee:        Отсечь хвост там, где оценки резко падают (knee cutoff).
                             Предпочтительнее фиксированного порога.
            knee_drop_ratio: Относительный перепад, считающийся «коленом».
        """
        key = str(path)
        if content is None:
            try:
                content = Path(path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                return []

        seen: dict[str, SimilarFile] = {}   # path → лучший найденный результат

        # Уровень 1: хэш-клоны
        sha = _sha256(content)
        for other in self._hash_index.get(sha, set()):
            if other != key:
                seen[other] = SimilarFile(other, 1.0, SimilarityLevel.HASH)

        # Уровень 2: структурные клоны
        fp = _ast_fingerprint(content, str(path))
        for other in self._fp_index.get(fp, set()):
            if other != key and other not in seen:
                seen[other] = SimilarFile(other, 0.95, SimilarityLevel.AST_EXACT)

        # Уровень 2.5: совпадение тела без комментариев и импортов
        bfp = _body_fingerprint(content, str(path))
        for other in self._body_index.get(bfp, set()):
            if other != key and other not in seen:
                seen[other] = SimilarFile(other, 0.92, SimilarityLevel.BODY_EXACT)

        # Уровень 3: AST-вектор
        qast = _ngrams(_ast_skeleton(content, str(path)))
        for other, pvec in self._ast_vecs.items():
            if other == key or other in seen:
                continue
            sim = _cosine(qast, pvec)
            if sim >= self.ast_threshold:
                seen[other] = SimilarFile(other, sim, SimilarityLevel.AST_VECTOR)

        # Уровень 4: контекст-вектор
        ctx_text = context if context is not None else _context_text(content, str(path))
        qctx = _ngrams(ctx_text)
        for other, pvec in self._ctx_vecs.items():
            if other == key or other in seen:
                continue
            sim = _cosine(qctx, pvec)
            if sim >= self.ctx_threshold:
                seen[other] = SimilarFile(other, sim, SimilarityLevel.CTX_VECTOR)

        candidates = sorted(seen.values())  # по убыванию similarity

        # Knee cutoff: обрезать хвост где оценки резко падают
        if use_knee and len(candidates) > 1:
            candidates = _knee_cutoff(candidates, knee_drop_ratio)

        # MMR: разнообразие (предотвращает N одинаковых клонов в выдаче)
        if use_mmr and len(candidates) > 1:
            candidates = _mmr_select(candidates, self._ast_vecs, mmr_lambda, top_k)
        else:
            candidates = candidates[:top_k]

        return candidates

    def cluster(self, threshold: float | None = None) -> list[list[str]]:
        """Транзитивные кластеры через Union-Find на уровне AST-вектора.

        В отличие от clone_groups() (только точные совпадения), учитывает
        транзитивность: если A≈B и B≈C выше порога, то {A, B, C} — один кластер.
        Порог по умолчанию = self.ast_threshold.
        """
        thr = threshold if threshold is not None else self.ast_threshold
        items = list(self._ast_vecs.items())
        if not items:
            return []

        # Union-Find с path compression
        parent: dict[str, str] = {p: p for p, _ in items}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(x: str, y: str) -> None:
            px, py = find(x), find(y)
            if px != py:
                parent[px] = py

        for i in range(len(items)):
            p1, v1 = items[i]
            for j in range(i + 1, len(items)):
                p2, v2 = items[j]
                if _cosine(v1, v2) >= thr:
                    union(p1, p2)

        groups: dict[str, list[str]] = {}
        for p, _ in items:
            groups.setdefault(find(p), []).append(p)

        return [sorted(g) for g in groups.values() if len(g) > 1]

    def clone_groups(self, level: SimilarityLevel = SimilarityLevel.HASH) -> list[list[str]]:
        """Вернуть группы клонов для заданного уровня точности."""
        if level == SimilarityLevel.HASH:
            return [sorted(paths) for paths in self._hash_index.values() if len(paths) > 1]
        elif level == SimilarityLevel.AST_EXACT:
            return [sorted(paths) for paths in self._fp_index.values() if len(paths) > 1]
        elif level == SimilarityLevel.BODY_EXACT:
            return [sorted(paths) for paths in self._body_index.values() if len(paths) > 1]
        return []

    def diverged_clones(
        self, ast_sim_threshold: float = 0.70, top_k_per_file: int = 5
    ) -> list[dict]:
        """Файлы, которые структурно похожи (ast_vector), но НЕ точные клоны (ast_exact разный).

        Это бывшие клоны, которые разошлись — намеренная кастомизация или забытый diff.
        Полезно для blast radius: изменение в одном таком файле может не затронуть «сестёр»,
        хотя интуитивно они кажутся одинаковыми.

        Returns:
            list[dict] с ключами:
              path_a, path_b  — пара расходящихся клонов
              ast_sim         — cosine-схожесть AST-векторов (структурное сходство)
              fp_a, fp_b      — разные AST-отпечатки (подтверждение расхождения)
        """
        items = [(p, v) for p, v in self._ast_vecs.items()]
        result: list[dict] = []
        seen: set[frozenset] = set()

        for i, (p1, v1) in enumerate(items):
            fp1 = self._file_fps.get(p1, "")
            count = 0
            for j, (p2, v2) in enumerate(items):
                if i == j:
                    continue
                fp2 = self._file_fps.get(p2, "")
                # Точные клоны нас не интересуют
                if fp1 and fp2 and fp1 == fp2:
                    continue
                pair = frozenset([p1, p2])
                if pair in seen:
                    continue
                sim = _cosine(v1, v2)
                if sim >= ast_sim_threshold:
                    seen.add(pair)
                    result.append({
                        "path_a":  p1,
                        "path_b":  p2,
                        "ast_sim": sim,
                        "fp_a":    fp1,
                        "fp_b":    fp2,
                    })
                    count += 1
                    if count >= top_k_per_file:
                        break

        return sorted(result, key=lambda x: -x["ast_sim"])

    def body_subgroups(self) -> list[dict]:
        """Для каждой ast_exact clone группы разбить на body_exact подгруппы.

        Пример: SecurityConfig.java × 29 (ast_exact) → 29 body_subgroups × 1
                Queues.java × 5 (ast_exact) → 2 body_subgroups: {3 файла} и {2 файла}

        Позволяет ответить на вопрос:
          «сигнатуры одинаковые — а тела (логика) насколько разошлись?»

        Returns:
            list[dict] с ключами:
              filename    — имя файла (basename)
              ast_group   — все пути с одинаковым ast_fp
              subgroups   — список body_exact подгрупп внутри этой ast_group
              body_vars   — кол-во разных вариантов тела
        """
        result = []
        for ast_fp, paths in self._fp_index.items():
            if len(paths) < 2:
                continue
            # Разбиваем на body_exact подгруппы
            body_to_paths: dict[str, list[str]] = {}
            for p in paths:
                bfp = self._file_body_fps.get(p, "")
                body_to_paths.setdefault(bfp, []).append(p)
            body_vars = len(body_to_paths)
            result.append({
                "filename":  Path(next(iter(paths))).name,
                "ast_group": sorted(paths),
                "subgroups": [sorted(g) for g in body_to_paths.values()],
                "body_vars": body_vars,
            })
        # Сортируем: сначала с наибольшим количеством вариантов тела
        return sorted(result, key=lambda x: (-x["body_vars"], -len(x["ast_group"])))

    def stats(self) -> dict:
        """Сводная статистика индекса."""
        ast_groups_with_body_diff = sum(
            1 for fp, paths in self._fp_index.items()
            if len(paths) > 1
            and len({self._file_body_fps.get(p, "") for p in paths}) > 1
        )
        return {
            "indexed_files":           len(self._file_hashes),
            "hash_clone_groups":       sum(1 for s in self._hash_index.values() if len(s) > 1),
            "ast_clone_groups":        sum(1 for s in self._fp_index.values()   if len(s) > 1),
            "body_clone_groups":       sum(1 for s in self._body_index.values() if len(s) > 1),
            "unique_hashes":           len(self._hash_index),
            "unique_ast_fps":          len(self._fp_index),
            "unique_body_fps":         len(self._body_index),
            # ast_exact клоны с разными телами методов (намеренная кастомизация)
            "ast_groups_body_diverged": ast_groups_with_body_diff,
        }

    # ─── Внутренние методы ────────────────────────────────────────────────────

    def _evict(self, key: str) -> None:
        """Удалить все записи одного файла из инвертированных индексов."""
        if key in self._file_hashes:
            old_sha = self._file_hashes.pop(key)
            bucket = self._hash_index.get(old_sha)
            if bucket:
                bucket.discard(key)
                if not bucket:
                    del self._hash_index[old_sha]

        if key in self._file_fps:
            old_fp = self._file_fps.pop(key)
            bucket = self._fp_index.get(old_fp)
            if bucket:
                bucket.discard(key)
                if not bucket:
                    del self._fp_index[old_fp]

        if key in self._file_body_fps:
            old_bfp = self._file_body_fps.pop(key)
            bucket = self._body_index.get(old_bfp)
            if bucket:
                bucket.discard(key)
                if not bucket:
                    del self._body_index[old_bfp]

        self._ast_vecs.pop(key, None)
        self._ctx_vecs.pop(key, None)


# ─── Вспомогательные функции (модульный уровень) ─────────────────────────────

def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _ngrams(text: str, n: int = 3) -> Counter:
    """Символьные n-граммы с нормализацией пробелов."""
    t = re.sub(r"\s+", " ", text.lower()).strip()
    return Counter(t[i : i + n] for i in range(max(0, len(t) - n + 1)))


def _cosine(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    dot = sum(a[k] * b[k] for k in a if k in b)
    mag = math.sqrt(sum(v * v for v in a.values())) * math.sqrt(
        sum(v * v for v in b.values())
    )
    return dot / mag if mag else 0.0


# Паттерны для извлечения структурного скелета
_STRUCT_PATTERNS: dict[str, re.Pattern] = {
    "java": re.compile(
        r"(?:@\w+(?:\([^)]*\))?\s*)*"
        r"(?:public|private|protected|static|final|abstract|synchronized\s+)*"
        r"(?:class|interface|enum|void|int|long|boolean|String|List|Map|Optional|"
        r"ResponseEntity|CompletableFuture)\s+\w+",
        re.MULTILINE,
    ),
    "python": re.compile(r"^(?:class|def|async def)\s+\w+", re.MULTILINE),
    "go":     re.compile(r"^(?:func|type|var|const)\s+\w+",  re.MULTILINE),
    "js":     re.compile(
        r"(?:function|class|const|let|var|export\s+(?:default\s+)?(?:function|class))\s+\w+",
        re.MULTILINE,
    ),
}

# Паттерны для контекстного текста
_IMPORT_RE     = re.compile(
    r"import\s+[\w.]+|require\s*\([\"'][\w./]+[\"']\)|from\s+[\w.]+\s+import",
    re.MULTILINE,
)
_HTTP_RE       = re.compile(r"(?:https?|ws)://[\w.\-:]+(?:/[\w/\-.?=&%]*)?")
_ANNOTATION_RE = re.compile(r"@\w+(?:\([\w\"./\s,=]*\))?")


def _detect_lang(path: str) -> str:
    return {
        ".java": "java", ".py": "python", ".go": "go",
        ".js": "js", ".ts": "js", ".tsx": "js",
    }.get(Path(path).suffix.lower(), "unknown")


def _ast_skeleton(content: str, path: str = "") -> str:
    """Структурный скелет файла: только сигнатуры деклараций без тел и литералов."""
    lang = _detect_lang(path)
    pat = _STRUCT_PATTERNS.get(lang)
    if pat:
        return "\n".join(pat.findall(content))
    # Fallback для неизвестных языков: убрать строки и сократить по границе строки
    stripped = re.sub(r'"[^"]*"|\'[^\']*\'|`[^`]*`', "", content)
    return truncate_code(stripped, 3000)


def _ast_fingerprint(content: str, path: str = "") -> str:
    """SHA-256 нормализованного скелета (сортировка строк → порядок методов не важен)."""
    skeleton = _ast_skeleton(content, path)
    # Сортировка строк: SecurityConfig с методами в разном порядке → одинаковый отпечаток
    normalized = "\n".join(sorted(set(skeleton.lower().splitlines())))
    return _sha256(normalized)


def _context_text(content: str, path: str = "") -> str:
    """Контекстный текст: импорты + HTTP-вызовы + аннотации = «что делает файл»."""
    parts: list[str] = []
    parts.extend(_IMPORT_RE.findall(content))
    parts.extend(_HTTP_RE.findall(content))
    parts.extend(_ANNOTATION_RE.findall(content))
    return " ".join(parts)


# Паттерны для удаления шума (комментарии + импорты)
_NOISE_PATTERNS: dict[str, list[re.Pattern]] = {
    "java": [
        re.compile(r"/\*.*?\*/", re.DOTALL),           # /* ... */ и javadoc
        re.compile(r"//.*$", re.MULTILINE),             # // строчный
        re.compile(r"^\s*(?:import|package)\s+.*?;\s*$", re.MULTILINE),  # import/package
    ],
    "python": [
        re.compile(r'""".*?"""', re.DOTALL),            # docstring """
        re.compile(r"'''.*?'''", re.DOTALL),            # docstring '''
        re.compile(r"#.*$", re.MULTILINE),              # # строчный
        re.compile(r"^\s*(?:import|from)\s+.*$", re.MULTILINE),          # import / from
    ],
    "go": [
        re.compile(r"/\*.*?\*/", re.DOTALL),
        re.compile(r"//.*$", re.MULTILINE),
        re.compile(r"^\s*(?:import|package)\s+.*$", re.MULTILINE),
    ],
    "js": [
        re.compile(r"/\*.*?\*/", re.DOTALL),
        re.compile(r"//.*$", re.MULTILINE),
        re.compile(r"^\s*(?:import|export\s+\{[^}]*\}\s+from|require)\s+.*?[;$]?$", re.MULTILINE),
    ],
}


def _strip_noise(content: str, path: str = "") -> str:
    """Удалить комментарии и строки импортов/пакетов.

    Возвращает нормализованное тело: только код без шапки.
    Используется для body_exact — сравнения файлов по логике, а не по стилю/организации.
    """
    lang = _detect_lang(path)
    patterns = _NOISE_PATTERNS.get(lang, _NOISE_PATTERNS["java"])
    text = content
    for pat in patterns:
        text = pat.sub("", text)
    # Схлопнуть множественные пустые строки после удаления шума
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _body_fingerprint(content: str, path: str = "") -> str:
    """SHA-256 тела файла без комментариев и импортов.

    Два файла с одинаковым body_fp но разным ast_fp отличаются только шапкой:
    объявлением пакета, import-секцией или комментариями. Логика кода идентична.
    """
    body = _strip_noise(content, path)
    # Нормализация пробелов, регистр не трогаем (имена методов важны)
    normalized = re.sub(r"[ \t]+", " ", body)
    return _sha256(normalized)
