"""Универсальный интерпретатор декларативных правил классификации.

Правила хранятся в domain_config.yaml:classification_rules.
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
