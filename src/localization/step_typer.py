"""Фаза 2.1 — Step Typing: классификация шага сценария.

Принимает action-текст шага → возвращает StepInfo.
При наличии StepParams от Qwen (Pass 1) использует их как основной сигнал;
иначе — regex-based fallback.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .._config import cfg_get, build_optional_re
from ..rule_engine import RuleEngine, TokenInjectionEngine, ComplexTokenEngine

if TYPE_CHECKING:
    from src.localization.qwen_encoder import StepParams


_SAFE_KEYWORD_TYPES  = frozenset(cfg_get("safe_keyword_step_types", []))
_QWEN_SVC_MAPPINGS   = cfg_get("qwen_service_mappings", {})


# ---------------------------------------------------------------------------
# Паттерны типа поезда (из domain_config.yaml → step_train_patterns)
#
# ВНИМАНИЕ: DongChe здесь G-диапазон для BM25-disambiguation.
# В oracle.py DongChe — D-тип (разные роли!).
# ---------------------------------------------------------------------------

def _build_train_re(key: str) -> re.Pattern[str]:
    """Собрать regex из конфига (step_train_patterns[key]). Ошибка если ключа нет."""
    raw = cfg_get("step_train_patterns", {})
    alts = raw.get(key)
    if not alts:
        raise ValueError(f"step_train_patterns.{key} не найден в domain_config.yaml")
    pattern = r"\b(" + "|".join(alts) + r")\b"
    return re.compile(pattern, re.I)


_G_TRAIN     = _build_train_re("G")
_OTHER_TRAIN = _build_train_re("OTHER")
# NOT_G: отрицание high-speed — приоритет над _G_TRAIN (как в oracle.py)
_NOT_G_TRAIN = _build_train_re("NOT_G")

_HAS_FOOD = build_optional_re("has_food")
_NO_FOOD  = build_optional_re("no_food")
_HAS_INS  = build_optional_re("has_insurance")
_NO_INS   = build_optional_re("no_insurance")
_HAS_LUG  = build_optional_re("has_luggage")
_NO_LUG   = build_optional_re("no_luggage")

# ---------------------------------------------------------------------------
# Правила классификации шагов (из domain_config.yaml → step_rules)
# ---------------------------------------------------------------------------

def _load_step_rules() -> list[tuple[str, list[str]]]:
    """Загрузить правила из конфига (fallback когда Qwen недоступен или вернул 'other')."""
    raw = cfg_get("step_rules", [])
    return [(r["type"], r["patterns"]) for r in raw]


_STEP_RULES: list[tuple[str, list[str]]] = _load_step_rules()

_CLASSIFIER       = RuleEngine(cfg_get("classification_rules", []))
_TOKEN_INJECTOR   = TokenInjectionEngine(cfg_get("token_injection_rules", []))
_COMPLEX_INJECTOR = ComplexTokenEngine(cfg_get("complex_token_rules", []))


# ---------------------------------------------------------------------------
# Dataclass результата
# ---------------------------------------------------------------------------


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
    # Дополнительные BM25-токены, выведенные из типа шага
    extra_tokens: list[str] = field(default_factory=list)


def _apply_train_type(text: str, info: StepInfo) -> None:
    if _NOT_G_TRAIN.search(text):
        info.train_type = "OTHER"
    elif _G_TRAIN.search(text):
        info.train_type = "G"
    elif _OTHER_TRAIN.search(text):
        info.train_type = "OTHER"


def _apply_options(text: str, info: StepInfo) -> None:
    """Regex baseline для optional addon атрибутов (из optional_patterns.json)."""
    _HAS = {"has_food": (_HAS_FOOD, _NO_FOOD),
            "has_insurance": (_HAS_INS, _NO_INS),
            "has_luggage": (_HAS_LUG, _NO_LUG)}
    for field_name, (pos, neg) in _HAS.items():
        if pos.search(text) and not neg.search(text):
            setattr(info, field_name, True)
        elif neg.search(text):
            setattr(info, field_name, False)


def _apply_qwen_params(step_params: "StepParams", info: StepInfo) -> None:
    """Применяет Qwen StepParams → StepInfo по qwen_service_mappings из конфига."""
    for field_name, cfg in _QWEN_SVC_MAPPINGS.items():
        keys = cfg["keys"] if isinstance(cfg, dict) else [cfg]
        combine = cfg.get("combine", "first") if isinstance(cfg, dict) else "first"
        vals = [step_params.get_service(k) for k in keys]
        non_none = [v for v in vals if v is not None]
        if not non_none:
            continue  # нет данных от Qwen → оставить regex-значение
        if combine == "any":
            setattr(info, field_name, any(v is True for v in vals)
                    if any(v is True for v in vals) else False)
        else:
            setattr(info, field_name, non_none[0])


def classify(action: str, step_params: "StepParams | None" = None) -> StepInfo:
    """Классифицировать текст шага, вернуть StepInfo.

    step_params: StepParams от Qwen Pass 1 (основной путь).
    Если None — regex-based fallback.
    """
    text = action.lower()
    info = StepInfo()

    # RuleEngine: is_refused, seat_class, pay_type — из domain_config.yaml:classification_rules
    _CLASSIFIER.evaluate_into(text, info)

    # train_type — всегда regex: D-train→G mapping намеренный, Qwen не знает этот трюк.
    # step_type — Qwen primary (NL-понимание шире regex), regex как fallback.
    _apply_train_type(text, info)

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

    # Regex baseline → Qwen override (если step_params доступен)
    _apply_options(text, info)
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

    # Qwen keywords: инжектировать только для безопасных step_types
    if (step_params is not None
            and step_params.keywords
            and info.step_type in _SAFE_KEYWORD_TYPES):
        for kw in step_params.keywords:
            if kw and kw not in tokens:
                tokens.append(kw)

    return tokens
