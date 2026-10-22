"""Универсальные движки декларативных правил.

Правила хранятся в domain_config.yaml.
Python-код не знает имён полей — только yaml знает.
"""
import re
from typing import Any


class RuleEngine:
    """Загружает правила из конфига, применяет к тексту через setattr."""

    def __init__(self, rules: list[dict]):
        sorted_rules = sorted(rules, key=lambda r: r.get("priority", 0), reverse=True)
        self._rules: list[dict] = []
        for r in sorted_rules:
            self._rules.append({
                "id":    r["rule_id"],
                "field": r["target_field"],
                "value": r["target_value"],
                "group": r.get("group"),
                "pos":   [re.compile(p, re.I) for p in r.get("positive_patterns", [])],
                "neg":   [re.compile(p, re.I) for p in r.get("negative_patterns", [])],
            })

    def evaluate_into(self, text: str, target: Any) -> None:
        """Применить правила к тексту, записать результаты в target через setattr.

        Правила внутри одной group взаимоисключающие (побеждает высший priority).
        """
        done_groups: set[str] = set()
        for rule in self._rules:
            g = rule["group"]
            if g and g in done_groups:
                continue
            if any(rx.search(text) for rx in rule["neg"]):
                continue
            if any(rx.search(text) for rx in rule["pos"]):
                setattr(target, rule["field"], rule["value"])
                if g:
                    done_groups.add(g)


class TokenInjectionEngine:
    """Генерирует BM25-токены по значениям полей объекта (token_injection_rules)."""

    def __init__(self, rules: list[dict]):
        self._rules = rules

    def collect_tokens(self, text: str, info: Any) -> list[str]:
        """Вернуть список токенов согласно правилам из конфига."""
        tokens: list[str] = []
        low = text.lower()
        for rule in self._rules:
            if getattr(info, rule["match_field"], None) != rule["match_value"]:
                continue
            cond = rule.get("text_contains")
            if cond and cond not in low:
                continue
            tokens.extend(rule["tokens"])
        return tokens


class ComplexTokenEngine:
    """Генерирует токены по составным правилам с приоритетным источником (complex_token_rules).

    priority_source: имя атрибута в step_params (Qwen-значение, приоритет)
    fallback_field:  имя поля в info (значение из RuleEngine, fallback)
    """

    def __init__(self, rules: list[dict]):
        self._rules = rules

    def collect_tokens(self, info: Any, step_params: Any = None) -> list[str]:
        tokens: list[str] = []
        for rule in self._rules:
            if getattr(info, rule["trigger_field"], None) != rule["trigger_value"]:
                continue
            ps = rule.get("priority_source", "")
            val: str = (getattr(step_params, ps, "") or "") if step_params else ""
            if not val:
                val = getattr(info, rule.get("fallback_field", ""), "") or ""
            mapped = rule.get("value_mapping", {}).get(val)
            if mapped:
                tokens.extend(mapped)
        return tokens
