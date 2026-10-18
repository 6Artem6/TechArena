"""Cross-encoder реранкер поверх BM25+BGE-M3 кандидатов.

Использует BAAI/bge-reranker-base: AutoModelForSequenceClassification,
принимает пары (query, doc) → scalar relevance logit.

Место в пайплайне:
  BM25 → BGE-M3 bi-encoder (top-20) → CrossEncoder → FlowContext → tau-окно

Преимущество перед bi-encoder: cross-attention видит оба текста одновременно —
правильно сопоставляет "rebook the ticket" ↔ "rebook" в имени метода,
"cancel" ↔ "cancelTicket" vs "calculate".

Без cross-encoder это решается extra_tokens из step_typer (hardcode).
"""
from __future__ import annotations

import logging
import math

_log = logging.getLogger(__name__)

_CE_MODEL = None
_CE_TOKENIZER = None
_CE_TRIED = False


def _try_load(model_name: str = "BAAI/bge-reranker-base") -> bool:
    """Лениво загрузить cross-encoder. Возвращает True если удалось."""
    global _CE_MODEL, _CE_TOKENIZER, _CE_TRIED
    if _CE_TRIED:
        return _CE_MODEL is not None
    _CE_TRIED = True
    try:
        from transformers import (  # type: ignore  # noqa: PLC0415
            AutoModelForSequenceClassification,
            AutoTokenizer,
        )
        _CE_TOKENIZER = AutoTokenizer.from_pretrained(model_name)
        _CE_MODEL = AutoModelForSequenceClassification.from_pretrained(model_name)
        _CE_MODEL.eval()
        _log.info("CrossEncoder: загружена модель %s", model_name)
        return True
    except ImportError:
        _log.debug("transformers не установлен — cross-encoder отключён")
    except Exception as exc:
        _log.warning("CrossEncoder: ошибка загрузки %s: %s", model_name, exc)
    return False


class CrossEncoder:
    """Переранжирование кандидатов через joint (query, doc) cross-attention.

    Формула:
        score = alpha * sigmoid(ce_logit) + (1 - alpha) * existing_norm

    alpha=0.7: cross-encoder доминирует, BM25+BGE-M3 как prior.
    Fallback: если модель не загружена — candidates без изменений.
    """

    def __init__(self, model_name: str = "BAAI/bge-reranker-base") -> None:
        self._model_name = model_name
        self._loaded = _try_load(model_name)

    @property
    def enabled(self) -> bool:
        return self._loaded and _CE_MODEL is not None

    def rerank(
        self,
        query: str,
        candidates: list[tuple[float, str]],
        doc_texts: dict[str, str],
        top_k: int = 20,
        alpha: float = 0.7,
    ) -> list[tuple[float, str]]:
        """Переранжировать кандидатов.

        Аргументы:
            query      — оригинальный текст шага (action)
            candidates — [(score, handler_id)] после BGE-M3
            doc_texts  — {handler_id: строка для cross-encoder}
            top_k      — сколько кандидатов cross-кодировать
            alpha      — вес cross-encoder (0 = BM25-only, 1 = чистый CE)
        """
        if not self.enabled or not candidates:
            return candidates

        top = candidates[:top_k]
        rest = candidates[top_k:]

        try:
            import torch  # type: ignore  # noqa: PLC0415

            queries = [query] * len(top)
            docs = [doc_texts.get(h, "") for _, h in top]

            enc = _CE_TOKENIZER(
                queries,
                docs,
                padding=True,
                truncation=True,
                max_length=512,
                return_tensors="pt",
            )
            with torch.no_grad():
                logits = _CE_MODEL(**enc).logits.squeeze(-1)

            # sigmoid: logit → [0, 1]
            raw = logits.tolist()
            if isinstance(raw, float):
                raw = [raw]
            ce_scores = [1.0 / (1.0 + math.exp(-x)) for x in raw]

            # Нормировать существующие баллы в [0, 1]
            ex_max = max(sc for sc, _ in top) or 1.0
            result: list[tuple[float, str]] = []
            for (ex_sc, h), ce_sc in zip(top, ce_scores):
                norm_ex = ex_sc / ex_max
                score = alpha * ce_sc + (1.0 - alpha) * norm_ex
                result.append((score, h))

            # Хвост (вне top_k): нормированные оценки, ниже чем у cross-coded
            if rest:
                tail_max = max(sc for sc, _ in rest) or 1.0
                ce_floor = min(sc for sc, _ in result) * 0.9
                for ex_sc, h in rest:
                    score = min((1.0 - alpha) * (ex_sc / tail_max), ce_floor)
                    result.append((score, h))

            return sorted(result, reverse=True)

        except Exception as exc:
            _log.debug("CrossEncoder.rerank() ошибка: %s — fallback", exc)
            return candidates
