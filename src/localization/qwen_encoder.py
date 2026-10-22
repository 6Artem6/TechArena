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
import pickle
import sys
import threading
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.error import URLError
from urllib.request import Request, urlopen

from pydantic import BaseModel, Field, ValidationError

if TYPE_CHECKING:
    from src.trace.oracle import Oracle

import os as _os
_OLLAMA_BASE_URL      = _os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
_OLLAMA_GENERATE_URL  = f"{_OLLAMA_BASE_URL}/api/generate"
_OLLAMA_TAGS_URL      = f"{_OLLAMA_BASE_URL}/api/tags"
_DEFAULT_OLLAMA_MODEL = _os.getenv("OLLAMA_MODEL", "qwen2.5-coder:3b")
_HF_MODEL_NAME        = _os.getenv("HF_QWEN_MODEL", "Qwen/Qwen2.5-Coder-1.5B-Instruct")
_TIMEOUT_S            = 10
_NUM_PREDICT          = 40   # индекс = 1-3 цифры, нет нужды в 160 токенах
_MIN_CONFIDENCE       = 0.55  # инжект только при confidence >= этого порога

def _qwen_cache_path() -> Path:
    from src._config import cfg_cache_dir
    return cfg_cache_dir() / "qwen_encoder_cache.pkl"


# ---------------------------------------------------------------------------
# Pydantic-контракт вывода — индекс лучшего handler-а
# ---------------------------------------------------------------------------

class QwenOutput(BaseModel):
    """Ответ Qwen: 1-based индекс лучшего handler-а + уверенность.

    index      — 1-based индекс из пронумерованного списка кандидатов.
                 0 = нет подходящего handler-а.
    confidence — уверенность модели (0.0-1.0).
                 При confidence < _MIN_CONFIDENCE инжект в граф не выполняется.
    """
    index: int = Field(
        default=0,
        ge=0,
        description="1-based handler index from numbered candidate list, 0=no match",
    )
    confidence: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Confidence 0-1 that the selected handler matches the action",
    )

    @classmethod
    def from_raw(cls, raw: dict) -> "QwenOutput":
        """Парсинг с fallback на defaults при невалидном ответе."""
        try:
            return cls.model_validate(raw)
        except (ValidationError, Exception):
            idx = raw.get("index", 0)
            try:
                idx = max(0, int(idx))
            except (TypeError, ValueError):
                idx = 0
            conf = float(raw.get("confidence", 0.5)) if "confidence" in raw else 0.5
            conf = max(0.0, min(1.0, conf))
            return cls(index=idx, confidence=conf)


# ---------------------------------------------------------------------------
# Pass 1: classify-only output — StepParams (структурированный контракт шага)
# ---------------------------------------------------------------------------

class ServiceItem(BaseModel):
    """Явно упомянутый сервис в тексте шага."""
    name: str     # food_train | food_station | insurance | luggage
    present: bool


class StepParams(BaseModel):
    """Результат Pass 1: полный структурированный контракт шага.

    Заменяет regex-based классификацию в step_typer.py.
    Все поля выводятся Qwen из natural language текста шага.
    """
    step_type:       str              = Field(default="other")
    train_type:      str              = Field(default="")    # "G" | "OTHER" | ""
    seat_class:      str              = Field(default="")    # "FIRST" | "SECOND" | ""
    services:        list[ServiceItem] = Field(default_factory=list)
    payment_mode:    str              = Field(default="")    # "internal" | "external" | ""
    search_criteria: list[str]        = Field(default_factory=list)  # cheapest/quickest/fewest_stops/transfer
    order_filter:    str              = Field(default="")    # "status:Paid" | "status:Cancel" | "date:travel" | ""
    actor_role:      str              = Field(default="")    # "passenger" | "admin" | ""
    keywords:        list[str]        = Field(default_factory=list)   # BM25 extra terms

    @classmethod
    def from_raw(cls, raw: dict) -> "StepParams":
        try:
            return cls.model_validate(raw)
        except Exception:
            st = str(raw.get("step_type", "other")).strip() or "other"
            return cls(step_type=st)

    def get_service(self, name: str) -> "bool | None":
        """True/False если сервис явно упомянут, None если нет данных."""
        for svc in self.services:
            if svc.name == name:
                return svc.present
        return None


# Обратная совместимость: старое имя класса → StepParams
QwenClassifyOutput = StepParams


_CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "step_type":       {"type": "string"},
        "train_type":      {"type": "string"},
        "seat_class":      {"type": "string"},
        "services": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name":    {"type": "string"},
                    "present": {"type": "boolean"},
                },
                "required": ["name", "present"],
            },
        },
        "payment_mode":    {"type": "string"},
        "search_criteria": {"type": "array", "items": {"type": "string"}},
        "order_filter":    {"type": "string"},
        "actor_role":      {"type": "string"},
        "keywords":        {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "step_type", "train_type", "seat_class", "services",
        "payment_mode", "search_criteria", "order_filter", "actor_role", "keywords",
    ],
}

_CLASSIFY_SYSTEM = (
    "You are a train ticket system action classifier.\n"
    "Extract these fields from the user action text:\n\n"
    "step_type: ONE of: login, register, book, pay, cancel, rebook, collect,\n"
    "  search_train, search_advanced, search_transit, view_trip, query_orders,\n"
    "  query_consign, add_consign, update_consign, add_contact, get_contact,\n"
    "  browse_food_station, query_food_trip, check_price, check_refund, query_insurance,\n"
    "  enter_station, admin_login, add_station, add_train_type, add_route, add_trip, add_price, other\n\n"
    "train_type: \"G\" (high-speed/bullet/gaotie/CRH/DongChe/D-type), "
    "\"OTHER\" (regular/slow/non-high-speed), \"\" (unknown)\n"
    "seat_class: \"FIRST\" (first-class/business), \"SECOND\" (second-class/economy), \"\" (unknown)\n"
    "services: list ONLY explicitly mentioned services:\n"
    "  food_train (food on the train), food_station (food at the station),\n"
    "  insurance/assurance, luggage/consign — each as {name, present:true/false}\n"
    "payment_mode: \"internal\" (internal balance), \"external\" (external service/balance empty), \"\" (unknown)\n"
    "search_criteria: mention any of: cheapest, quickest, fewest_stops, transfer\n"
    "order_filter: \"status:Paid\", \"status:Cancel\", \"date:travel\", \"date:purchase\", or \"\"\n"
    "actor_role: \"admin\" (admin context), \"passenger\" (user context), \"\" (unknown)\n"
    "keywords: 0-3 BM25 terms for finding the API handler (no city names, no train numbers)\n\n"
    "RULES:\n"
    "  search_transit = ONLY when transfer/connection/via/layover explicitly mentioned\n"
    "  search_advanced = cheapest/quickest/fewest-stops (no transfer)\n"
    "  services: omit unmentioned services (keep array short)\n"
    "  keywords: for pay add payment signal; for book add seat/service signals; skip for search\n\n"
    "Examples:\n"
    '  "book first-class with food on train and insurance" → '
    '{"step_type":"book","train_type":"","seat_class":"FIRST",'
    '"services":[{"name":"food_train","present":true},{"name":"insurance","present":true}],'
    '"payment_mode":"","search_criteria":[],"order_filter":"","actor_role":"passenger",'
    '"keywords":["first class","food","insurance"]}\n'
    '  "pay using internal account balance" → '
    '{"step_type":"pay","train_type":"","seat_class":"",'
    '"services":[],"payment_mode":"internal","search_criteria":[],"order_filter":"","actor_role":"",'
    '"keywords":["inside"]}\n'
    '  "pay using external payment service" → '
    '{"step_type":"pay","train_type":"","seat_class":"",'
    '"services":[],"payment_mode":"external","search_criteria":[],"order_filter":"","actor_role":"",'
    '"keywords":["payment","service"]}\n'
    '  "advanced search: cheapest G-train trips" → '
    '{"step_type":"search_advanced","train_type":"G","seat_class":"",'
    '"services":[],"payment_mode":"","search_criteria":["cheapest"],"order_filter":"","actor_role":"",'
    '"keywords":[]}\n'
    '  "ask for trips with a transfer via Suzhou" → '
    '{"step_type":"search_transit","train_type":"","seat_class":"",'
    '"services":[],"payment_mode":"","search_criteria":["transfer"],"order_filter":"","actor_role":"",'
    '"keywords":["transfer"]}\n'
    '  "query G-train orders (status: Paid)" → '
    '{"step_type":"query_orders","train_type":"G","seat_class":"",'
    '"services":[],"payment_mode":"","search_criteria":[],"order_filter":"status:Paid","actor_role":"",'
    '"keywords":[]}\n'
    "Output ONLY valid JSON. No other text."
)

_CLASSIFY_NUM_PREDICT = 200  # полный StepParams JSON ~60-120 токенов


# ---------------------------------------------------------------------------
# JSON schema для ollama structured output
# ---------------------------------------------------------------------------

_OLLAMA_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "index": {
            "type": "integer",
            "minimum": 0,
            "description": "1-based handler index, 0 if no match",
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": "Confidence 0-1 that selected handler matches action",
        },
    },
    "required": ["index", "confidence"],
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
        "Given a user action and a numbered list of handler methods, "
        "select the index of the BEST matching handler.\n\n"
        "Rules:\n"
        "  1. Match action verb/noun semantically to handler name.\n"
        "  2. confidence: 1.0 if name clearly matches action, 0.5 if uncertain, 0.2 if no match.\n"
        "  3. index=0 if NO handler in the list matches the action intent.\n\n"
        "Examples:\n"
        '  action "find trip with transfer via Suzhou", [3] travel2: getTransferResult\n'
        '    → {"index": 3, "confidence": 0.9}\n'
        '  action "fewest stops trip", [5] travel: getByMinStation GET /api/v1/travel/minStation\n'
        '    → {"index": 5, "confidence": 0.85}\n'
        '  action "board train with order", [2] execute: executeTicket POST /api/v1/execute\n'
        '    → {"index": 2, "confidence": 0.9}\n'
        '  action "pay for order via internal balance", [4] inside-payment: payOrder\n'
        '    → {"index": 4, "confidence": 0.8}\n'
        'Output ONLY valid JSON: {"index": N, "confidence": F}. No other text.'
    )
    user_tmpl = (
        'Action: "{action}"\n\n'
        "Session history (recent steps → found handlers):\n{history}\n\n"
        "Handler candidates (select the BEST index, 0 if none match):\n{handlers}\n\n"
        'JSON: {{"index": N, "confidence": 0.0-1.0}}'
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
        self._cache: dict[tuple, object] = self._load_cache()
        self._cache_lock = threading.Lock()
        self._language = _detect_language(targets_root)
        self._system, self._user_tmpl = _build_prompts(self._language)
        self._domain_context = self._load_domain_context()
        self._backend, self.enabled = self._init_backend()
        self._cache_dirty = False

    @staticmethod
    def _load_domain_context() -> str:
        """Загрузить domain context из project_passport.json для classify_step()."""
        try:
            from src._config import cfg_cache_dir
            pp_path = cfg_cache_dir() / "project_passport.json"
            if not pp_path.exists():
                return ""
            pp = json.loads(pp_path.read_text(encoding="utf-8"))
            domain = pp.get("domain", "")
            features = pp.get("main_features", [])
            parts: list[str] = []
            if domain:
                parts.append(f"System context: {domain}.")
            if features:
                parts.append("Business features: " + "; ".join(features[:3]) + ".")
            ctx = " ".join(parts)
            if ctx:
                print(f"[qwen] domain context loaded: {domain[:50]}", file=sys.stderr)
            return ctx
        except Exception:
            return ""

    @staticmethod
    def _load_cache() -> "dict[tuple, object]":
        try:
            with open(_qwen_cache_path(), "rb") as fh:
                data = pickle.load(fh)
            # Принимаем QwenOutput (handler selection) и QwenClassifyOutput (Pass 1)
            valid = {
                k: v for k, v in data.items()
                if isinstance(v, (QwenOutput, QwenClassifyOutput))
            }
            print(f"[qwen] кеш загружен: {len(valid)} записей", file=sys.stderr)
            return valid
        except Exception:
            return {}

    def _save_cache(self) -> None:
        try:
            _qwen_cache_path().parent.mkdir(parents=True, exist_ok=True)
            with open(_qwen_cache_path(), "wb") as fh:
                pickle.dump(self._cache, fh)
        except Exception as e:
            print(f"[qwen] кеш не сохранён: {e}", file=sys.stderr)

    # ------------------------------------------------------------------

    def _init_backend(self) -> tuple[str, bool]:
        # 1. Попробовать ollama
        try:
            with urlopen(Request(_OLLAMA_TAGS_URL), timeout=3) as resp:
                tags = json.loads(resp.read())
            names = {m["name"] for m in tags.get("models", [])}
            # Точное совпадение: модель должна присутствовать в списке
            if self.model in names:
                print(f"[qwen] backend=ollama, модель={self.model}", file=sys.stderr)
                return "ollama", True
            # Fallback: ближайшая модель той же семьи (предпочитаем более крупные)
            family = self.model.split(":")[0]
            fallback = next(
                (n for n in sorted(names, reverse=True) if n.startswith(family)),
                None,
            )
            if fallback is None:
                fallback = next((n for n in sorted(names, reverse=True) if "qwen2.5-coder" in n), None)
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

    def _call_ollama(
        self,
        prompt: str,
        schema: dict | None = None,
        system: str | None = None,
        num_predict: int | None = None,
        timeout: int | None = None,
    ) -> dict:
        payload = json.dumps({
            "model":   self.model,
            "system":  system if system is not None else self._system,
            "prompt":  prompt,
            "format":  schema if schema is not None else _OLLAMA_JSON_SCHEMA,
            "stream":  False,
            "options": {"temperature": 0, "seed": 42, "num_predict": num_predict or _NUM_PREDICT},
        }).encode()
        req = Request(
            _OLLAMA_GENERATE_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urlopen(req, timeout=timeout or _TIMEOUT_S) as resp:
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
        candidates: "list[tuple[int, str, str]]",
    ) -> QwenOutput:
        """Выбрать лучший handler по индексу из пронумерованного списка кандидатов.

        candidates: [(idx, module_short, display_str), ...]
        """
        if not self.enabled or not candidates:
            return QwenOutput()

        history_lines = []
        for rec in (step_history[-3:] if step_history else []):
            if getattr(rec, "handler_names", None):
                names_str = ", ".join(rec.handler_names[:4])
            else:
                names_str = ", ".join(sorted(rec.reachable_modules)[:3]) or "—"
            history_lines.append(f"  step_{rec.step_idx} {rec.step_type}: {names_str}")
        history_str = "\n".join(history_lines) or "  (начало сессии)"

        handlers_lines = "\n".join(
            f"  [{idx}] {mod}: {display}"
            for idx, mod, display in candidates
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
            # Bounds check: индекс должен быть в диапазоне [0, max_idx]
            max_idx = max((idx for idx, _, _ in candidates), default=0)
            if result.index > max_idx:
                result = QwenOutput(index=0, confidence=0.0)
        except Exception:
            result = QwenOutput()

        self._cache[cache_key] = result
        self._cache_dirty = True
        # Периодическое сохранение: каждые 5 новых запросов
        if len(self._cache) % 5 == 0:
            self._save_cache()
            self._cache_dirty = False
        return result

    # ------------------------------------------------------------------
    # Pass 1: параллельная классификация шагов (без кандидатов)
    # ------------------------------------------------------------------

    def classify_step(self, action: str) -> StepParams:
        """Pass 1: извлечь StepParams из текста шага через Qwen.

        Не требует списка кандидатов → можно запускать параллельно до Pass 2.
        Результат кешируется по тексту шага (ключ cls_v2).
        """
        cache_key = ("cls_v3", action)
        cached = self._cache.get(cache_key)
        if isinstance(cached, StepParams):
            return cached

        if not self.enabled:
            return StepParams()

        prompt = f'Action: "{action}"\n\nJSON:'
        classify_system = (
            (self._domain_context + "\n\n" + _CLASSIFY_SYSTEM)
            if self._domain_context else _CLASSIFY_SYSTEM
        )
        try:
            if self._backend == "ollama":
                raw = self._call_ollama(
                    prompt,
                    schema=_CLASSIFY_SCHEMA,
                    system=classify_system,
                    num_predict=_CLASSIFY_NUM_PREDICT,
                )
            elif self._backend == "transformers":
                raw = self._call_transformers(prompt)
            else:
                raw = {}
            result = StepParams.from_raw(raw)
        except Exception:
            result = StepParams()

        with self._cache_lock:
            self._cache[cache_key] = result
            self._cache_dirty = True
            if len(self._cache) % 5 == 0:
                self._save_cache()
                self._cache_dirty = False
        return result

    def batch_classify_steps(
        self,
        actions: list[str],
        n_workers: int = 4,
    ) -> list[StepParams]:
        """Pass 1: параллельная классификация всех шагов сценария.

        Вызовы независимы (не используют step_history) → ThreadPoolExecutor.
        Уже закешированные шаги не требуют вызова Qwen.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        results: list[StepParams] = [StepParams()] * len(actions)
        uncached_indices: list[int] = []

        for i, action in enumerate(actions):
            cached = self._cache.get(("cls_v3", action))
            if isinstance(cached, StepParams):
                results[i] = cached
            else:
                uncached_indices.append(i)

        if not uncached_indices or not self.enabled:
            n_cached = len(actions) - len(uncached_indices)
            if actions:
                print(
                    f"[qwen] classify: {n_cached}/{len(actions)} из кеша",
                    file=sys.stderr,
                )
            return results

        print(
            f"[qwen] classify: {len(actions) - len(uncached_indices)} из кеша, "
            f"{len(uncached_indices)} новых (workers={n_workers})",
            file=sys.stderr,
        )

        with ThreadPoolExecutor(max_workers=n_workers) as ex:
            futures = {
                ex.submit(self.classify_step, actions[i]): i
                for i in uncached_indices
            }
            for future in as_completed(futures):
                idx = futures[future]
                try:
                    results[idx] = future.result()
                except Exception:
                    pass

        return results

    def flush(self) -> None:
        """Принудительно сохранить кеш на диск."""
        if self._cache_dirty:
            self._save_cache()
            self._cache_dirty = False


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


def get_candidate_handlers_indexed(
    oracle: "Oracle",
    ix,
    max_per_service: int = 4,
    max_services: int = 8,
) -> "tuple[list[tuple[int, str, str]], dict[int, str]]":
    """Пронумерованный список handler-ов для Qwen index-based selection.

    Returns:
        display_list  — [(idx, module_short, display_str), ...] для промпта
        nid_map       — {idx: nid} для прямого инжекта без graph_name_lookup
    """
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

    reachable_mods: list[str] = []
    if getattr(oracle, "step_history", None):
        for mod in sorted(oracle.step_history[-1].reachable_modules):
            if mod not in seen_mods:
                reachable_mods.append(mod)
                seen_mods.add(mod)

    selected = (recent_mods + reachable_mods)[:max_services]

    by_mod: dict[str, list[tuple[str, str, str, str]]] = {}
    for nid, verb, path_list in ix.handlers:
        mod = ix.module.get(nid, "")
        if mod not in selected:
            continue
        name = ix.N.get(nid, {}).get("name", "")
        if not name:
            continue
        path_str = path_list[0] if path_list else ""
        by_mod.setdefault(mod, []).append((nid, name, verb, path_str))

    display_list: list[tuple[int, str, str]] = []
    nid_map: dict[int, str] = {}
    idx = 1
    for mod in selected:
        if mod not in by_mod:
            continue
        short = mod.replace("ts-", "").replace("-service", "")
        for nid, name, verb, path_str in by_mod[mod][:max_per_service]:
            display = f"{name} {verb} {path_str}" if verb and path_str else name
            display_list.append((idx, short, display))
            nid_map[idx] = nid
            idx += 1

    return display_list, nid_map


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
