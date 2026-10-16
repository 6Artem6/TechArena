"""Паспорт сервиса — REDUCE-агрегация Finding'ов одного сервиса.

Роль в pipeline:
  MAP   = запуск адаптеров на файлах → list[Finding]   (уже есть)
  REDUCE Phase 1 = ServicePassport.from_findings()     (этот модуль)
  REDUCE Phase 2 = PassportRegistry.build_call_graph() (этот модуль)

Паспорт — компактная выжимка: что сервис делает, куда ходит, на чём стоит.
PassportRegistry используется для двухуровневого blast-radius запроса:
  Level-1 coarse: scan ~68 паспортов, O(n) → сервисы-кандидаты
  Level-2 fine:   drill в файлы только кандидатов → конкретные точки
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Iterable

from ..adapters.tools.base import Finding, FindingKind


# ─── Структура паспорта ───────────────────────────────────────────────────────

@dataclass
class ServicePassport:
    """Паспорт одного сервиса — агрегация всех Finding'ов сервиса."""

    name: str                               # ts-travel-service
    tech_stack: list[str]  = field(default_factory=list)  # ["web:spring", "db:mysql"]
    endpoints: list[dict]  = field(default_factory=list)  # [{path, method, auth}]
    calls_out: list[str]   = field(default_factory=list)  # -> исходящие вызовы
    calls_in: list[str]    = field(default_factory=list)  # <- заполняется PassportRegistry
    db_deps: list[str]     = field(default_factory=list)  # "mysql:ts-travel-mysql:3306"
    mq_deps: list[str]     = field(default_factory=list)  # "amqp:rabbitmq:5672"
    ports: list[int]       = field(default_factory=list)  # [14567]
    env_keys: list[str]    = field(default_factory=list)  # ["TRAIN_SERVICE_HOST", ...]
    proxy_routes: list[dict] = field(default_factory=list) # nginx proxy_pass маршруты
    clone_fp: str = ""                      # AST-отпечаток из SimilarityIndex
    files_count: int = 0
    findings_count: int = 0

    # ─── Построение из Finding'ов ─────────────────────────────────────────────

    @classmethod
    def from_findings(cls, name: str, findings: Iterable[Finding]) -> "ServicePassport":
        """REDUCE Phase 1: собрать паспорт из всех Finding одного сервиса.

        Дедупликация: endpoint-пути и caller→callee уникализируются через set().
        Теряется только порядок — приемлемо для паспорта.
        """
        tech_set: set[str] = set()
        endpoint_set: set[tuple] = set()
        calls_set: set[str] = set()
        db_set: set[str] = set()
        mq_set: set[str] = set()
        port_set: set[int] = set()
        env_set: set[str] = set()
        proxy_list: list[dict] = []
        total = 0

        for f in findings:
            total += 1
            k = f.kind

            if k == FindingKind.HTTP_ENDPOINT:
                path = f.data.get("path", "")
                method = f.data.get("method", "*")
                if path:
                    endpoint_set.add((path, method))

            elif k == FindingKind.CALLS_SERVICE:
                callee = f.data.get("service") or f.data.get("callee", "")
                if callee and callee != name:
                    calls_set.add(callee)

            elif k == FindingKind.CALLS_DB:
                proto = f.data.get("protocol", "db")
                host = f.data.get("host", "")
                port = f.data.get("port", 0)
                if host:
                    db_set.add(f"{proto}:{host}:{port}" if port else f"{proto}:{host}")

            elif k == FindingKind.ASYNC_CALL:
                proto = f.data.get("protocol", "mq")
                host = f.data.get("host", "") or f.data.get("broker", "")
                port = f.data.get("port", 0)
                if host:
                    mq_set.add(f"{proto}:{host}:{port}" if port else f"{proto}:{host}")

            elif k == FindingKind.IMPORT:
                hint = f.data.get("tech_hint", "")
                if hint:
                    tech_set.add(hint)

            elif k == FindingKind.SERVICE_DEF:
                svc_port = f.data.get("port")
                if isinstance(svc_port, int) and svc_port > 0:
                    port_set.add(svc_port)
                # docker-compose/k8s ports могут быть списком
                for p in f.data.get("ports", []):
                    if isinstance(p, int):
                        port_set.add(p)
                    elif isinstance(p, str) and ":" in p:
                        try:
                            port_set.add(int(p.split(":")[-1].split("/")[0]))
                        except ValueError:
                            pass

            elif k == FindingKind.ENV_ACCESS:
                key = f.data.get("key", "")
                if key:
                    env_set.add(key)

            elif k == FindingKind.PROXY_ROUTE:
                location = f.data.get("location", "")
                target_svc = f.data.get("service", "")
                if location and target_svc:
                    proxy_list.append({
                        "location": location,
                        "service": target_svc,
                        "port": f.data.get("port", 0),
                    })

        return cls(
            name=name,
            tech_stack=sorted(tech_set),
            endpoints=[{"path": p, "method": m} for p, m in sorted(endpoint_set)],
            calls_out=sorted(calls_set),
            db_deps=sorted(db_set),
            mq_deps=sorted(mq_set),
            ports=sorted(port_set),
            env_keys=sorted(env_set),
            proxy_routes=proxy_list,
            findings_count=total,
        )

    # ─── Текстовое представление для векторизации ─────────────────────────────

    def to_text(self) -> str:
        """Компактный текст паспорта для n-граммного вектора / BGE-M3.

        Формат: пространство-разделённые токены вида category:value.
        Достаточно для cosine-сравнения двух паспортов без запуска ML.
        """
        parts: list[str] = [f"svc:{self.name}"]
        for t in self.tech_stack:
            parts.append(f"tech:{t}")
        for ep in self.endpoints:
            # Нормализуем path: убираем path params {id} → {p} для обобщения
            norm_path = re.sub(r"\{[^}]+\}", "{p}", ep["path"])
            parts.append(f"ep:{norm_path}")
        for c in self.calls_out:
            parts.append(f"calls:{c}")
        for d in self.db_deps:
            parts.append(f"db:{d.split(':')[1] if ':' in d else d}")
        for m in self.mq_deps:
            parts.append(f"mq:{m.split(':')[0]}")
        for p in self.ports:
            parts.append(f"port:{p}")
        return " ".join(parts)

    def vector(self) -> Counter:
        """n-грамм вектор паспорта (character trigrams)."""
        return _ngrams(self.to_text())

    # ─── Метрика схожести паспортов ───────────────────────────────────────────

    def similarity_to(self, other: "ServicePassport") -> float:
        """Cosine-схожесть двух паспортов через n-граммы."""
        return _cosine(self.vector(), other.vector())


# ─── Реестр паспортов — REDUCE Phase 2 ───────────────────────────────────────

class PassportRegistry:
    """Реестр всех сервисных паспортов репозитория.

    После добавления всех паспортов вызвать build_call_graph() —
    это заполнит calls_in для каждого сервиса (кто его вызывает).
    """

    def __init__(self) -> None:
        self._passports: dict[str, ServicePassport] = {}
        # Инвертированный индекс: service → set(callers)
        self._callers: dict[str, set[str]] = defaultdict(set)

    def add(self, passport: ServicePassport) -> None:
        """Добавить паспорт; предыдущий для того же имени заменяется."""
        self._passports[passport.name] = passport

    def build_call_graph(self) -> None:
        """REDUCE Phase 2: заполнить calls_in и построить инвертированный индекс."""
        self._callers.clear()
        for svc, p in self._passports.items():
            for callee in p.calls_out:
                self._callers[callee].add(svc)
        for p in self._passports.values():
            p.calls_in = sorted(self._callers.get(p.name, set()))

    def get(self, name: str) -> ServicePassport | None:
        return self._passports.get(name)

    def all_names(self) -> list[str]:
        return list(self._passports.keys())

    # ─── Двухуровневый blast radius ───────────────────────────────────────────

    def affected_by(self, service: str, depth: int = 2) -> dict[str, int]:
        """Level-1 coarse BFS по паспортам: какие сервисы зависят от `service`.

        Возвращает {service_name: hop_distance} — только прямые зависимости
        по calls_in (не файлы). Быстрый O(n·depth) без чтения файлов.

        Args:
            service: изменившийся сервис
            depth:   глубина обхода по calls_in цепочке
        """
        visited: dict[str, int] = {}
        frontier = {service}
        for hop in range(1, depth + 1):
            next_frontier: set[str] = set()
            for svc in frontier:
                p = self._passports.get(svc)
                if p is None:
                    continue
                for caller in p.calls_in:
                    if caller not in visited and caller != service:
                        visited[caller] = hop
                        next_frontier.add(caller)
            frontier = next_frontier
        return visited

    def similar_services(
        self, name: str, top_k: int = 5, threshold: float = 0.55
    ) -> list[tuple[str, float]]:
        """Семантически похожие сервисы по n-граммам паспорта (без BGE-M3).

        Полезно для обнаружения сервисов-кандидатов на blast-radius
        через структурное сходство (аналог clone_groups в SimilarityIndex).
        """
        src = self._passports.get(name)
        if src is None:
            return []
        qvec = src.vector()
        results: list[tuple[str, float]] = []
        for other_name, other_p in self._passports.items():
            if other_name == name:
                continue
            sim = _cosine(qvec, other_p.vector())
            if sim >= threshold:
                results.append((other_name, sim))
        return sorted(results, key=lambda x: -x[1])[:top_k]

    def stats(self) -> dict:
        total = len(self._passports)
        with_callers = sum(1 for p in self._passports.values() if p.calls_in)
        leaf_services = sum(1 for p in self._passports.values() if not p.calls_in)
        return {
            "services": total,
            "with_incoming_calls": with_callers,
            "leaf_services": leaf_services,
            "total_endpoints": sum(len(p.endpoints) for p in self._passports.values()),
            "total_call_edges": sum(len(p.calls_out) for p in self._passports.values()),
        }


# ─── Вспомогательные функции ─────────────────────────────────────────────────

def _ngrams(text: str, n: int = 3) -> Counter:
    t = re.sub(r"\s+", " ", text.lower()).strip()
    return Counter(t[i : i + n] for i in range(max(0, len(t) - n + 1)))


def _cosine(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    dot = sum(a[k] * b[k] for k in a if k in b)
    mag = math.sqrt(sum(v * v for v in a.values())) * math.sqrt(
        sum(v * v for v in b.values())
    )
    return dot / mag if mag else 0.0
