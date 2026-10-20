"""Семантический кодировщик для переранжирования entry-point кандидатов.

Реализует гибридный поиск (P-82): alpha * dense + (1-alpha) * BM25.

При наличии sentence-transformers + torch использует BGE-M3 (BAAI/bge-m3).
При отсутствии зависимостей — прозрачный fallback на BM25-only ранжирование
(SemanticReranker.rerank() возвращает candidates без изменений).

Использование:
    reranker = SemanticReranker()
    # После BM25 scorer.rank(q):
    candidates = reranker.rerank(action_text, candidates, doc_texts, alpha=0.5)

    # Проверить доступность ML:
    if reranker.enabled:
        print("BGE-M3 активен")
    else:
        print("BM25-only fallback")
"""
from __future__ import annotations

import logging

_log = logging.getLogger(__name__)

# Кеш модели (singleton для процесса)
_MODEL = None
_MODEL_TRIED = False


def _try_load_model(model_name: str = "BAAI/bge-m3"):
    """Лениво загрузить sentence-transformers модель.

    Возвращает модель или None если зависимости не установлены.
    Загрузка происходит один раз — результат кешируется.
    """
    global _MODEL, _MODEL_TRIED
    if _MODEL_TRIED:
        return _MODEL
    _MODEL_TRIED = True
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
        _MODEL = SentenceTransformer(model_name)
        _log.info("SemanticReranker: загружена модель %s", model_name)
    except ImportError:
        _log.debug("sentence-transformers не установлен — BM25-only fallback")
    except Exception as exc:
        _log.warning("SemanticReranker: ошибка загрузки %s: %s", model_name, exc)
    return _MODEL


class SemanticReranker:
    """Гибридное переранжирование BM25-кандидатов через dense-эмбеддинги.

    Алгоритм (P-82):
      score = alpha * cosine(q_emb, doc_emb) + (1-alpha) * bm25_norm

    Если модель недоступна — возвращает candidates без изменений.
    """

    def __init__(self, model_name: str = "BAAI/bge-m3") -> None:
        self._model_name = model_name
        self._model = _try_load_model(model_name)
        # Предвычисленные doc vectors: {handler_id → ndarray}.
        # Заполняется через precompute_doc_vecs() один раз при инициализации.
        self._doc_vecs: "dict[str, object]" = {}

    def precompute_doc_vecs(self, doc_texts: "dict[str, str]") -> None:
        """Предвычислить эмбеддинги всех handler doc_texts один раз.

        После вызова rerank() берёт векторы из кеша вместо повторного encode.
        Вызывать один раз после построения doc_texts (в init_enhanced_ground).
        """
        if not self.enabled or not doc_texts:
            return
        try:
            ids = list(doc_texts.keys())
            texts = [doc_texts[h] for h in ids]
            vecs = self._model.encode(
                texts,
                normalize_embeddings=True,
                batch_size=64,
                show_progress_bar=False,
            )
            self._doc_vecs = dict(zip(ids, vecs))
        except Exception as exc:
            _log.debug("precompute_doc_vecs ошибка: %s — кеш не заполнен", exc)

    @property
    def enabled(self) -> bool:
        """True если BGE-M3 (или другая модель) загружена и готова."""
        return self._model is not None

    def rerank(
        self,
        query_text: str,
        candidates: list[tuple[float, str]],
        doc_texts: dict[str, str],
        top_k: int = 20,
        alpha: float = 0.5,
    ) -> list[tuple[float, str]]:
        """Переранжировать кандидатов: alpha*dense + (1-alpha)*bm25_norm.

        Аргументы:
            query_text  — оригинальный текст шага (не токенизированный)
            candidates  — [(bm25_score, handler_id)] от Scorer.rank()
            doc_texts   — {handler_id: текст для кодирования}
            top_k       — сколько кандидатов брать из BM25-топа для dense
            alpha       — вес dense-компоненты (0 = чистый BM25, 1 = чистый dense)

        Возвращает:
            [(hybrid_score, handler_id)] отсортированные по убыванию.
            При ошибке или отсутствии модели — исходный candidates без изменений.
        """
        if not self.enabled or not candidates:
            return candidates

        try:
            import numpy as np  # type: ignore

            model = self._model

            # Нормировать BM25-баллы в [0, 1]
            bm_scores = [sc for sc, _ in candidates]
            bm_max = max(bm_scores) if bm_scores else 1.0
            norm_bm = {h: sc / bm_max for sc, h in candidates}

            # Взять top_k кандидатов для dense-кодирования
            top_cands = candidates[:top_k]
            doc_ids = [h for _, h in top_cands]

            q_emb = model.encode(query_text, normalize_embeddings=True)

            if self._doc_vecs:
                # Быстрый путь: берём предвычисленные векторы из кеша
                import numpy as np  # noqa: F811
                cached = [self._doc_vecs.get(h) for h in doc_ids]
                if all(v is not None for v in cached):
                    d_embs = np.stack(cached)
                else:
                    # Часть handlers не в кеше → encode только недостающие
                    texts = [doc_texts.get(h, "") for h in doc_ids]
                    d_embs = model.encode(texts, normalize_embeddings=True,
                                         batch_size=32, show_progress_bar=False)
            else:
                texts = [doc_texts.get(h, "") for h in doc_ids]
                if not any(texts):
                    return candidates
                d_embs = model.encode(
                    texts,
                    normalize_embeddings=True,
                    batch_size=32,
                    show_progress_bar=False,
                )

            # Cosine similarity (нормализованные векторы → dot product)
            dense_scores: list[float] = (d_embs @ q_emb).tolist()

            # Нормировать dense-баллы в [0, 1]
            d_max = max(dense_scores) if dense_scores else 1.0
            norm_dense = {
                h: sc / d_max
                for h, sc in zip(doc_ids, dense_scores)
            }

            # Гибридная оценка
            combined: list[tuple[float, str]] = [
                (alpha * norm_dense.get(h, 0.0) + (1 - alpha) * norm_bm.get(h, 0.0), h)
                for _, h in top_cands
            ]
            return sorted(combined, reverse=True)

        except Exception as exc:
            _log.debug("SemanticReranker.rerank() ошибка: %s — fallback BM25", exc)
            return candidates

    def encode_batch(
        self,
        texts: list[str],
        batch_size: int = 32,
    ) -> "list | None":
        """Закодировать батч текстов в нормализованные эмбеддинги.

        Возвращает numpy-массив или None если модель недоступна.
        """
        if not self.enabled:
            return None
        try:
            return self._model.encode(
                texts,
                normalize_embeddings=True,
                batch_size=batch_size,
                show_progress_bar=False,
            )
        except Exception as exc:
            _log.debug("SemanticReranker.encode_batch() ошибка: %s", exc)
            return None
