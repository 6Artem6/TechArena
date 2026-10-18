"""Code-специфичные энкодеры для NL→Code retrieval.

CodeBERT (microsoft/codebert-base):
  - Bimodal pretraining: (NL docstring, Java/Python/Go code) пары с GitHub
  - Задача: NL Code Search — найти функцию по текстовому описанию
  - Подходит: action text → Java handler signature

GraphCodeBERT (microsoft/graphcodebert-base):
  - То же, что CodeBERT + data flow graph pretraining
  - Лучше для code-to-code задач (clone detection, refinement)
  - Без DFG (только сигнатуры) — преимущество над CodeBERT минимально

Оба используются как bi-encoders:
  encode(NL query) → embedding
  encode(code doc) → embedding
  score = cosine(q_emb, doc_emb)
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np  # type: ignore

_log = logging.getLogger(__name__)

# Кеш синглтонов: model_name → CodeEncoder
_INSTANCES: dict[str, "CodeEncoder"] = {}


def get_encoder(model_name: str) -> "CodeEncoder":
    """Получить/создать синглтон энкодера для данного model_name."""
    if model_name not in _INSTANCES:
        _INSTANCES[model_name] = CodeEncoder(model_name)
    return _INSTANCES[model_name]


class CodeEncoder:
    """Bi-encoder поверх HuggingFace трансформера для NL↔Code совместимости.

    Использует CLS-токен последнего скрытого слоя как представление.
    Предварительно вычисляет эмбеддинги всех обработчиков (precompute),
    затем при scoring кодирует только запрос.

    Совместим с CodeBERT и GraphCodeBERT (одинаковая RoBERTa архитектура).
    """

    def __init__(self, model_name: str) -> None:
        self._model_name = model_name
        self._model = None
        self._tokenizer = None
        self._doc_embs: "dict[str, object]" = {}  # handler_id → numpy array (hidden,)
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            from transformers import AutoTokenizer, AutoModel  # type: ignore
            import torch  # type: ignore
            self._tokenizer = AutoTokenizer.from_pretrained(self._model_name)
            self._model = AutoModel.from_pretrained(self._model_name)
            self._model.eval()
            _log.info("CodeEncoder: загружена %s", self._model_name)
        except Exception as exc:
            _log.warning("CodeEncoder: не удалось загрузить %s: %s", self._model_name, exc)

    @property
    def enabled(self) -> bool:
        self._load()
        return self._model is not None

    def _encode_texts(self, texts: list[str], max_length: int = 256) -> "np.ndarray":
        """Батч-кодирование текстов → нормализованные CLS-эмбеддинги."""
        import numpy as np
        import torch

        all_embs: list = []
        batch_size = 32
        for i in range(0, len(texts), batch_size):
            batch = texts[i: i + batch_size]
            enc = self._tokenizer(
                batch,
                padding=True,
                truncation=True,
                max_length=max_length,
                return_tensors="pt",
            )
            with torch.no_grad():
                out = self._model(**enc)
            cls = out.last_hidden_state[:, 0, :]  # (batch, hidden)
            norms = cls.norm(dim=-1, keepdim=True).clamp(min=1e-9)
            cls_norm = (cls / norms).cpu().numpy()
            all_embs.append(cls_norm)
        return np.vstack(all_embs) if all_embs else np.empty((0, 0))

    def precompute(self, doc_texts: dict[str, str]) -> None:
        """Предвычислить эмбеддинги всех обработчиков (вызывать один раз при старте).

        doc_texts: {handler_id → code-ориентированный текст}
        Например: "POST /api/v1/travel/query queryInfo TripInfo TravelController ts-travel-service"
        """
        if not self.enabled or not doc_texts:
            return
        import numpy as np

        ids = list(doc_texts.keys())
        texts = [doc_texts[i] for i in ids]
        embs = self._encode_texts(texts)
        self._doc_embs = {h: embs[j] for j, h in enumerate(ids)}
        _log.info(
            "CodeEncoder [%s]: предвычислено %d эмбеддингов",
            self._model_name.split("/")[-1],
            len(ids),
        )

    def score_query(self, query: str, handler_ids: list[str]) -> dict[str, float]:
        """Вернуть косинусные сходства запроса с обработчиками.

        Возвращает {handler_id → cosine_score}, только для ids из handler_ids.
        """
        if not self.enabled or not self._doc_embs:
            return {}
        import numpy as np

        q_emb = self._encode_texts([query])[0]  # (hidden,)
        result: dict[str, float] = {}
        for h in handler_ids:
            doc_emb = self._doc_embs.get(h)
            if doc_emb is not None:
                result[h] = float(np.dot(q_emb, doc_emb))
        return result

    def rerank(
        self,
        query: str,
        candidates: list[tuple[float, str]],
        gamma: float = 0.3,
    ) -> list[tuple[float, str]]:
        """Переранжировать кандидатов: (1-gamma)*existing + gamma*code_score.

        Оба слагаемых нормализованы в [0, 1] перед смешиванием.
        gamma=0 → без изменений, gamma=1 → чистый code score.
        """
        if not self.enabled or not self._doc_embs or not candidates:
            return candidates

        handler_ids = [h for _, h in candidates]
        code_scores = self.score_query(query, handler_ids)
        if not code_scores:
            return candidates

        # Нормировать существующие оценки
        existing_max = max(sc for sc, _ in candidates) or 1.0
        # Нормировать code scores в [0,1] min-max
        vals = list(code_scores.values())
        c_min, c_max = min(vals), max(vals)
        c_range = c_max - c_min if c_max > c_min else 1.0

        combined: list[tuple[float, str]] = []
        for sc, h in candidates:
            norm_sc = sc / existing_max
            raw_c = code_scores.get(h, c_min)
            norm_c = (raw_c - c_min) / c_range
            combined.append(((1.0 - gamma) * norm_sc + gamma * norm_c, h))

        return sorted(combined, reverse=True)
