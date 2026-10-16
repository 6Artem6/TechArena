"""SymbolIndex — resolve статических строковых констант (task 10).

Цель: `Queues.queueName` → `"email"` через индекс по всем Java-классам репо.

Паттерн в train-ticket:
  Queues.java (несколько) содержат `public static final String queueName = "email"`.
  convertAndSend(Queues.queueName, ...)  →  нужно знать что queueName = "email".

Индекс хранит:
  ClassName.FIELD → "value"
  package.ClassName.FIELD → "value"

resolve("Queues.queueName") → "email"
resolve("preserveOther.config.Queues.queueName") → "email"

Коллизии (5 Queues.java): берём значение из того же сервиса;
если нет — первое найденное.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class _Entry:
    value: str
    service: str
    fqn: str  # полный FQN: package.ClassName


class SymbolIndex:
    """Индекс статических строковых констант Java-классов."""

    def __init__(self) -> None:
        # simple_key (ClassName.FIELD) → list[_Entry]
        self._simple: dict[str, list[_Entry]] = {}
        # fqn_key (pkg.Class.FIELD) → _Entry
        self._fqn: dict[str, _Entry] = {}

    def build_from_parsed(self, parsed_files: list) -> "SymbolIndex":
        """Построить индекс из уже распарсенных ParsedJavaFile."""
        for pf in parsed_files:
            for cls in getattr(pf, "classes", []):
                fqn = cls.fqn  # package.ClassName
                simple = fqn.rsplit(".", 1)[-1]
                for field_name, value in cls.static_constants.items():
                    entry = _Entry(value=value, service=pf.service, fqn=fqn)
                    # Полный FQN ключ
                    self._fqn[f"{fqn}.{field_name}"] = entry
                    # Простой ключ: ClassName.FIELD
                    key = f"{simple}.{field_name}"
                    self._simple.setdefault(key, []).append(entry)
        return self

    def resolve(self, expr: str, service: str = "") -> str | None:
        """Resolve выражение вида 'ClassName.FIELD' или 'pkg.Class.FIELD'.

        Приоритет:
          1. Точное совпадение по FQN
          2. Простое имя (ClassName.FIELD) — предпочитает тот же сервис
          3. None если не найдено
        """
        expr = expr.strip()

        # Полный FQN
        entry = self._fqn.get(expr)
        if entry:
            return entry.value

        # Простой ClassName.FIELD
        entries = self._simple.get(expr)
        if entries:
            # Предпочтение тому же сервису
            for e in entries:
                if e.service == service:
                    return e.value
            return entries[0].value

        return None

    @property
    def stats(self) -> dict[str, int]:
        return {
            "symbol_fqn_entries": len(self._fqn),
            "symbol_simple_entries": len(self._simple),
        }
