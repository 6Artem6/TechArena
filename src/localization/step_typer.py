"""Фаза 2.1 — Step Typing: классификация шага сценария.

Принимает action-текст шага → возвращает StepInfo.
Вся бизнес-логика (train_type, optional add-ons, step_rules) вынесена в domain_config.yaml
и cache/optional_patterns.json. Python-код полностью агностичен к домену.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .._config import cfg_get, cfg_cache_dir, load_optional_classification_rules
from ..rule_engine import RuleEngine, TokenInjectionEngine, ComplexTokenEngine

if TYPE_CHECKING:
    from src.localization.qwen_encoder import StepParams


_SAFE_KEYWORD_TYPES = frozenset(cfg_get("safe_keyword_step_types", []))
_QWEN_SVC_MAPPINGS  = cfg_get("qwen_service_mappings", {})


def _load_step_token_bridges() -> dict[str, list[str]]:
    """Загрузить BM25 co-occurrence bridges из кеша (gen_step_token_bridges.py).

    Возвращает {} если кеш отсутствует — graceful degradation.
    """
    import pickle as _pickle
    cache_path = cfg_cache_dir() / "step_token_bridges.pkl"
    if not cache_path.exists():
        return {}
    try:
        return _pickle.loads(cache_path.read_bytes())
    except Exception:
        return {}


_STEP_TOKEN_BRIDGES: dict[str, list[str]] = _load_step_token_bridges()


def _load_step_rules() -> list[tuple[str, list[str]]]:
    """Загрузить правила fallback для step_type из конфига."""
    raw = cfg_get("step_rules", [])
    return [(r["type"], r["patterns"]) for r in raw]


_STEP_RULES: list[tuple[str, list[str]]] = _load_step_rules()

# Универсальные движки из domain_config.yaml + cache/optional_patterns.json
_CLASSIFIER = RuleEngine(
    cfg_get("classification_rules", []) + load_optional_classification_rules()
)
_TOKEN_INJECTOR   = TokenInjectionEngine(cfg_get("token_injection_rules", []))
_COMPLEX_INJECTOR = ComplexTokenEngine(cfg_get("complex_token_rules", []))


@dataclass
class StepInfo:
    step_type: str = "unknown"
    train_type: str = ""         # "G" | "OTHER" | ""
    seat_class: str = ""         # "FIRST" | "SECOND" | ""
    pay_type: str = ""           # "internal" | "external" | ""
    has_food: bool | None = None
    has_insurance: bool | None = None
    has_luggage: bool | None = None
    is_refused: bool = False
    extra_tokens: list[str] = field(default_factory=list)


def _apply_qwen_params(step_params: "StepParams", info: StepInfo) -> None:
    """Применяет Qwen StepParams → StepInfo по qwen_service_mappings из конфига."""
    for field_name, cfg in _QWEN_SVC_MAPPINGS.items():
        keys    = cfg["keys"] if isinstance(cfg, dict) else [cfg]
        combine = cfg.get("combine", "first") if isinstance(cfg, dict) else "first"
        vals     = [step_params.get_service(k) for k in keys]
        non_none = [v for v in vals if v is not None]
        if not non_none:
            continue
        if combine == "any":
            setattr(info, field_name, any(v is True for v in vals)
                    if any(v is True for v in vals) else False)
        else:
            setattr(info, field_name, non_none[0])


def classify(action: str, step_params: "StepParams | None" = None) -> StepInfo:
    """Классифицировать текст шага, вернуть StepInfo."""
    text = action.lower()
    info = StepInfo()

    # 1. Все атрибуты (train_type, seat_class, pay_type, has_food, has_insurance,
    #    has_luggage, is_refused) — декларативно через RuleEngine.
    _CLASSIFIER.evaluate_into(text, info)

    # 2. step_type: Qwen primary, regex fallback
    if step_params is not None and step_params.step_type not in ("other", "unknown", ""):
        info.step_type = step_params.step_type
    else:
        for step_type, keywords in _STEP_RULES:
            for kw in keywords:
                if re.search(kw, text, re.I):
                    info.step_type = step_type
                    break
            if info.step_type != "unknown":
                break

    # 3. Qwen override для сервисов (если доступен)
    if step_params is not None:
        _apply_qwen_params(step_params, info)

    info.extra_tokens = _build_extra_tokens(info, action, step_params)
    return info


def _build_extra_tokens(
    info: StepInfo,
    action: str = "",
    step_params: "StepParams | None" = None,
) -> list[str]:
    """BM25-токены из StepInfo атрибутов — полностью data-driven из domain_config.yaml."""
    tokens = _TOKEN_INJECTOR.collect_tokens(action, info)
    tokens += _COMPLEX_INJECTOR.collect_tokens(info, step_params)

    if (step_params is not None
            and step_params.keywords
            and info.step_type in _SAFE_KEYWORD_TYPES):
        for kw in step_params.keywords:
            if kw and kw not in tokens:
                tokens.append(kw)

    return tokens
