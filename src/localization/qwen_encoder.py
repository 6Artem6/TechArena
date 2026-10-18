"""Qwen2.5-Coder query decomposer — два бэкенда: ollama и transformers.

Pipeline:
  1. decompose(action, step_history, candidates) → QwenOutput
  2. graph_name_lookup(bm25_tokens, ix, active_modules) → set[nid]
     Прямой граф-поиск: find handlers where name matches Qwen token.
     Сильнее BM25-boost: не через scoring, а exact-name injection в hs.

Бэкенды (в порядке приоритета):
  1. ollama /api/generate (если daemon запущен)
  2. transformers AutoModelForCausalLM (если модель в HF cache)
  3. disabled (fallback — без LLM)

Контракт вывода: QwenOutput (Pydantic) с confidence-фильтрацией инжекта.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.error import URLError
from urllib.request import Request, urlopen

from pydantic import BaseModel, Field, ValidationError

if TYPE_CHECKING:
    from src.trace.oracle import Oracle

_OLLAMA_GENERATE_URL = "http://localhost:11434/api/generate"
_OLLAMA_TAGS_URL     = "http://localhost:11434/api/tags"
_DEFAULT_OLLAMA_MODEL = "qwen2.5-coder:3b"
_HF_MODEL_NAME       = "Qwen/Qwen2.5-Coder-1.5B-Instruct"
_TIMEOUT_S           = 10
_NUM_PREDICT         = 160
_MIN_CONFIDENCE      = 0.55  # инжект только при confidence >= этого порога


# ---------------------------------------------------------------------------
# Pydantic-контракт вывода — структурированный ответ Qwen
# ---------------------------------------------------------------------------

class QwenOutput(BaseModel):
    """Структурированный ответ Qwen на запрос декомпозиции шага.

    bm25      — точные имена handler-ов (lowercase) + ключевые глаголы действия.
                Пример: ["gettransferresult", "transfer", "via"]
    query     — NL-запрос для semantic search; все доменные слова сохранены.
                Пример: "find trip with transfer at intermediate station"
    confidence — уверенность модели в совпадении bm25-токенов с action (0.0-1.0).
                 При confidence < _MIN_CONFIDENCE инжект в граф не выполняется.
    """
    bm25: list[str] = Field(
        default_factory=list,
        description=(
            "3-5 items: EXACT handler method name (lowercase) matching action "
            "+ key action verbs. Example: [\"gettransferresult\", \"transfer\"]"
        ),
    )
    query: str = Field(
        default="",
        description=(
            "One NL sentence for semantic search. "
            "Keep ALL domain terms (transfer, fewest-stops, intermediate, etc)."
        ),
    )
    confidence: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Confidence that bm25 tokens correctly match the action (0.0-1.0).",
    )

    @classmethod
    def from_raw(cls, raw: dict) -> "QwenOutput":
        """Парсинг с fallback на defaults при невалидном ответе."""
        try:
            return cls.model_validate(raw)
        except (ValidationError, Exception):
            # Попробовать спасти хотя бы bm25 и query
            raw_bm25 = raw.get("bm25", [])
            bm25 = [str(t).strip().lower() for t in raw_bm25 if t][:6] if isinstance(raw_bm25, list) else []
            query = str(raw.get("query", "")).strip()
            conf = float(raw.get("confidence", 0.5)) if "confidence" in raw else 0.5
            conf = max(0.0, min(1.0, conf))
            return cls(bm25=bm25, query=query, confidence=conf)


# ---------------------------------------------------------------------------
# JSON schema для ollama structured output
# ---------------------------------------------------------------------------

_OLLAMA_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "bm25": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 6,
            "description": "Exact handler method names (lowercase) + key action verbs",
        },
        "query": {
            "type": "string",
            "description": "NL search query keeping ALL domain terms",
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": "Confidence 0-1 that bm25 matches action",
        },
    },
    "required": ["bm25", "query", "confidence"],
}


# ---------------------------------------------------------------------------
# Определение языка проекта
# ---------------------------------------------------------------------------

def _detect_language(targets_root: "Path | None") -> str:
    """Определить язык проекта по расширениям файлов в targets dir."""
    if targets_root is None:
        return "Java"
    ext_map = {".java": "Java", ".go": "Go", ".py": "Python",
               ".ts": "TypeScript", ".js": "JavaScript", ".kt": "Kotlin"}
    counts: Counter = Counter()
    try:
        for f in Path(targets_root).rglob("*"):
            if f.is_file() and f.suffix.lower() in ext_map:
                counts[f.suffix.lower()] += 1
    except Exception:
        pass
    if counts:
        return ext_map[counts.most_common(1)[0][0]]
    return "Java"


# ---------------------------------------------------------------------------
# Промпты с примерами и JSON-схемой
# ---------------------------------------------------------------------------

def _build_prompts(language: str) -> tuple[str, str]:
    system = (
        f"You are a {language} microservice expert.\n"
        "Given a user action and handler method names from nearby services, "
        "find the BEST matching handler and return structured JSON.\n\n"
        "Rules:\n"
        "  1. Scan handler names for words matching the action — include EXACT name (lowercase) in bm25.\n"
        "  2. Include key action verbs (transfer, cheapest, min, execute, pay, etc.) in bm25.\n"
        "  3. query: keep ALL domain terms; do NOT drop words like 'transfer', 'fewest', 'via'.\n"
        "  4. confidence: 1.0 if handler name obviously matches, 0.5 if uncertain, 0.2 if no match.\n\n"
        "Examples:\n"
        '  action "transfer trip via Suzhou" + handler "getTransferResult"\n'
        '    → {"bm25": ["gettransferresult", "transfer", "via"], "query": "find trip with transfer via intermediate station", "confidence": 0.9}\n'
        '  action "fewest stops" + handler "getByMinStation"\n'
        '    → {"bm25": ["getbyminstation", "min"], "query": "search trips with minimum stops fewest stations", "confidence": 0.85}\n'
        '  action "board train with order" + handler "executeTicket"\n'
        '    → {"bm25": ["executeticket", "execute", "board"], "query": "enter station board train execute ticket", "confidence": 0.9}\n'
        '  action "pay for order" + handler "payOrder"\n'
        '    → {"bm25": ["pay", "order", "payment"], "query": "pay order from account balance", "confidence": 0.8}\n'
        'Output ONLY valid JSON matching the schema. No other text.'
    )
    user_tmpl = (
        'Action: "{action}"\n\n'
        "Session history (recent steps → found handlers):\n{history}\n\n"
        "Handler method names in graph-relevant services\n"
        "(format: name VERB /path or just name):\n{handlers}\n\n"
        "JSON output with fields: bm25 (array), query (string), confidence (0.0-1.0)"
    )
    return system, user_tmpl


# ---------------------------------------------------------------------------
# QwenEncoder — ollama или transformers
# ---------------------------------------------------------------------------

class QwenEncoder:
    def __init__(
        self,
        model: str = _DEFAULT_OLLAMA_MODEL,
        targets_root: "Path | None" = None,
    ):
        self.model = model
        self._cache: dict[tuple, QwenOutput] = {}
        self._language = _detect_language(targets_root)
        self._system, self._user_tmpl = _build_prompts(self._language)
        self._backend, self.enabled = self._init_backend()

    # ------------------------------------------------------------------

    def _init_backend(self) -> tuple[str, bool]:
        # 1. Попробовать ollama
        try:
            with urlopen(Request(_OLLAMA_TAGS_URL), timeout=3) as resp:
                tags = json.loads(resp.read())
            names = {m["name"] for m in tags.get("models", [])}
            if any(self.model in n or n.startswith(self.model.split(":")[0]) for n in names):
                print(f"[qwen] backend=ollama, модель={self.model}", file=sys.stderr)
                return "ollama", True
            fallback = next((n for n in sorted(names) if "qwen2.5-coder" in n), None)
            if fallback:
                self.model = fallback
                print(f"[qwen] backend=ollama, fallback={self.model}", file=sys.stderr)
                return "ollama", True
        except Exception:
            pass

        # 2. Попробовать transformers
        try:
            self._tf_model, self._tf_tok = _load_transformers_model(_HF_MODEL_NAME)
            print(f"[qwen] backend=transformers, model={_HF_MODEL_NAME}", file=sys.stderr)
            return "transformers", True
        except Exception as e:
            print(f"[qwen] transformers недоступен: {e}", file=sys.stderr)

        print("[qwen] оба бэкенда недоступны — query decomposition отключена", file=sys.stderr)
        return "none", False

    # ------------------------------------------------------------------

    def _call_ollama(self, prompt: str) -> dict:
        payload = json.dumps({
            "model":   self.model,
            "system":  self._system,
            "prompt":  prompt,
            "format":  _OLLAMA_JSON_SCHEMA,
            "stream":  False,
            "options": {"temperature": 0, "num_predict": _NUM_PREDICT},
        }).encode()
        req = Request(
            _OLLAMA_GENERATE_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urlopen(req, timeout=_TIMEOUT_S) as resp:
            data = json.loads(resp.read())
        return json.loads(data.get("response", "{}"))

    def _call_transformers(self, prompt: str) -> dict:
        tok, model = self._tf_tok, self._tf_model
        messages = [
            {"role": "system", "content": self._system},
            {"role": "user", "content": prompt},
        ]
        text = tok.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        inputs = tok([text], return_tensors="pt").to(model.device)
        import torch
        with torch.no_grad():
            out_ids = model.generate(
                **inputs,
                max_new_tokens=_NUM_PREDICT,
                do_sample=False,
                temperature=1.0,
                pad_token_id=tok.eos_token_id,
            )
        new_ids = out_ids[:, inputs["input_ids"].shape[1]:]
        raw = tok.batch_decode(new_ids, skip_special_tokens=True)[0].strip()
        start = raw.find("{")
        end = raw.rfind("}") + 1
        return json.loads(raw[start:end]) if start >= 0 else {}

    # ------------------------------------------------------------------

    def decompose(
        self,
        action: str,
        step_history: list,
        candidates: dict[str, list[str]],
    ) -> QwenOutput:
        if not self.enabled:
            return QwenOutput()

        # История: handler names если есть, иначе module names
        history_lines = []
        for rec in (step_history[-3:] if step_history else []):
            if getattr(rec, "handler_names", None):
                names_str = ", ".join(rec.handler_names[:4])
            else:
                names_str = ", ".join(sorted(rec.reachable_modules)[:3]) or "—"
            history_lines.append(f"  step_{rec.step_idx} {rec.step_type}: {names_str}")
        history_str = "\n".join(history_lines) or "  (начало сессии)"

        handlers_lines = "\n".join(
            f"  {mod}: {', '.join(names)}"
            for mod, names in sorted(candidates.items())
        )
        prompt = self._user_tmpl.format(
            action=action,
            history=history_str,
            handlers=handlers_lines or "  (нет контекста)",
        )

        cache_key = (action, history_str, handlers_lines)
        if cache_key in self._cache:
            return self._cache[cache_key]

        try:
            if self._backend == "ollama":
                raw = self._call_ollama(prompt)
            elif self._backend == "transformers":
                raw = self._call_transformers(prompt)
            else:
                raw = {}

            result = QwenOutput.from_raw(raw)
            # Нормализовать bm25 токены
            result.bm25 = [
                str(t).strip().lower()
                for t in result.bm25
                if t and len(str(t).strip()) > 2
            ][:6]
        except Exception:
            result = QwenOutput()

        self._cache[cache_key] = result
        return result


# ---------------------------------------------------------------------------
# Ленивая загрузка transformers-модели (singleton)
# ---------------------------------------------------------------------------

_TF_MODEL = None
_TF_TOK   = None


def _load_transformers_model(model_name: str):
    global _TF_MODEL, _TF_TOK
    if _TF_MODEL is not None:
        return _TF_MODEL, _TF_TOK
    from transformers import AutoTokenizer, AutoModelForCausalLM
    import torch
    _TF_TOK   = AutoTokenizer.from_pretrained(model_name)
    _TF_MODEL = AutoModelForCausalLM.from_pretrained(
        model_name,
        torch_dtype=torch.float16 if torch.cuda.is_available() else torch.float32,
        device_map="auto",
    )
    _TF_MODEL.eval()
    return _TF_MODEL, _TF_TOK


# ---------------------------------------------------------------------------
# Graph-guided candidate collector — recent-first ordering + HTTP paths
# ---------------------------------------------------------------------------

def get_candidate_handlers(
    oracle: "Oracle",
    ix,
    max_per_service: int = 4,
    max_services: int = 8,
) -> dict[str, list[str]]:
    """Handler names (+ HTTP verb/path если доступно) по релевантным сервисам.

    Формат элемента: "handlerName VERB /path/template"
    или просто "handlerName" если path неизвестен.

    Порядок: recent-first (недавно найденные сервисы первыми).
    Алфавитная сортировка НЕ используется — отрезает travel-plan (8-й по алфавиту).
    """
    # 1. Сервисы из последних шагов (most recent first)
    recent_mods: list[str] = []
    seen_mods: set[str] = set()
    for rec in reversed(getattr(oracle, "step_history", [])):
        for nid in rec.found_handlers:
            mod = ix.module.get(nid, "")
            if mod and mod not in seen_mods:
                recent_mods.append(mod)
                seen_mods.add(mod)
        if len(recent_mods) >= 4:
            break

    # 2. Graph-reachable из последнего шага
    reachable_mods: list[str] = []
    if getattr(oracle, "step_history", None):
        for mod in sorted(oracle.step_history[-1].reachable_modules):
            if mod not in seen_mods:
                reachable_mods.append(mod)
                seen_mods.add(mod)

    selected = (recent_mods + reachable_mods)[:max_services]

    # Индексируем handlers по модулю: {mod: [(nid, verb, path_str), ...]}
    by_mod: dict[str, list[tuple[str, str, str]]] = {}
    for nid, verb, path_list in ix.handlers:
        mod = ix.module.get(nid, "")
        if mod not in selected:
            continue
        name = ix.N.get(nid, {}).get("name", "")
        if not name:
            continue
        # Первый путь из path_list, если есть
        path_str = path_list[0] if path_list else ""
        by_mod.setdefault(mod, []).append((nid, name, verb, path_str))

    result: dict[str, list[str]] = {}
    for mod in selected:
        if mod not in by_mod:
            continue
        short = mod.replace("ts-", "").replace("-service", "")
        entries = []
        for _nid, name, verb, path_str in by_mod[mod][:max_per_service]:
            if verb and path_str:
                entries.append(f"{name} {verb} {path_str}")
            else:
                entries.append(name)
        result[short] = entries

    return result


# ---------------------------------------------------------------------------
# Graph name lookup — инжект handler-ов по точному совпадению имени
# ---------------------------------------------------------------------------

def graph_name_lookup(
    bm25_tokens: list[str],
    ix,
    active_modules: "set[str]",
    reachable_modules: "set[str] | None" = None,
) -> set[str]:
    """Найти handler nid-ы по точному совпадению имени с Qwen bm25-токеном.

    Сильнее BM25-boost: не через scoring, а direct injection в hs.
    Фильтр: только handlers из активных или reachable модулей.

    Пример:
      bm25=["gettransferresult"] → travel-plan::getTransferResult → nid
    """
    if not bm25_tokens:
        return set()

    relevant = active_modules.copy()
    if reachable_modules:
        relevant |= reachable_modules

    matched: set[str] = set()
    for tok in bm25_tokens:
        tok_norm = tok.lower().replace("-", "").replace("_", "")
        if len(tok_norm) < 4:
            continue
        for nid, _verb, _tpl in ix.handlers:
            mod = ix.module.get(nid, "")
            if relevant and mod not in relevant:
                continue
            name = ix.N.get(nid, {}).get("name", "").lower()
            if tok_norm in name or name in tok_norm:
                matched.add(nid)

    return matched
