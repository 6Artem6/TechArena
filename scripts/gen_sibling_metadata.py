"""Оффлайн-генерация метаданных сиблинг-пар сервисов.

Запуск: python scripts/gen_sibling_metadata.py [--force]

Находит пары похожих сервисов через detect_all_siblings() и классифицирует:
  g_other:      ts-order ↔ ts-order-other  → {is_g_side: true/false}
  func_variant: ts-route ↔ ts-route-plan   → {is_basic_side: true/false}

Qwen классифицирует g_other пары через архитектурно-нейтральный промпт
(PRIMARY/STANDARD vs ALTERNATIVE/SECONDARY). Fallback — структурные эвристики
(суффикс -other, цифра, длина имени). func_variant — только эвристики.

Результат: cache/sibling_metadata.json
  {
    "ts-order-service": {
      "variant_group": "order",
      "variant_type": "g_other",
      "is_g_side": true,
      "sibling": "ts-order-other-service"
    },
    ...
  }
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

from src.graph.siblings import detect_all_siblings, _strip  # noqa: E402

SKEL_PATH   = ROOT / "tt-challenge" / "skeleton" / "trainticket.json"
PASSPORT_PATH = ROOT / "cache" / "service_passports.pkl"
CACHE_PATH  = ROOT / "cache" / "sibling_metadata.json"

_VARIANT_CLASSIFY_SYSTEM = (
    "You are a microservice architecture analyst.\n"
    "Given two related services/modules and their method descriptions, determine which one "
    "represents the PRIMARY/STANDARD implementation and which represents the "
    "ALTERNATIVE, SECONDARY, or EXTENDED variant.\n"
    "PRIMARY/STANDARD: serves the default, most common, or mainstream use case.\n"
    "ALTERNATIVE/SECONDARY: serves a specialized, overflow, or non-default variant.\n"
    "Output strict JSON only:\n"
    '{"primary_side": "A" or "B", "confidence": 0-1, "reason": "one sentence"}\n'
    "If you cannot determine, set confidence < 0.5."
)

_FUNC_CLASSIFY_SYSTEM = (
    "You are a microservice architecture analyst.\n"
    "Given two related services/modules and their method descriptions, determine which one "
    "provides the CORE/DIRECT capability and which provides the EXTENDED/COMPOSITE capability.\n"
    "CORE/DIRECT: handles the fundamental, single-step operation.\n"
    "EXTENDED/COMPOSITE: handles the derived, multi-step, or planning operation.\n"
    "Output strict JSON only:\n"
    '{"core_side": "A" or "B", "confidence": 0-1, "reason": "one sentence"}'
)

_VARIANT_SCHEMA = {
    "type": "object",
    "properties": {
        "primary_side": {"type": "string"},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["primary_side", "confidence"],
}

_FUNC_SCHEMA = {
    "type": "object",
    "properties": {
        "core_side": {"type": "string"},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["core_side", "confidence"],
}


# ---------------------------------------------------------------------------
# Паспортный контекст
# ---------------------------------------------------------------------------

_TARGETS_DIR = ROOT / "tt-challenge" / "targets" / "trainticket"


def _load_passports() -> dict[str, dict]:
    """Вернуть {module_name: {method: {summary, synonyms}}}."""
    import pickle
    if not PASSPORT_PATH.exists():
        return {}
    with open(PASSPORT_PATH, "rb") as f:
        raw = pickle.load(f)
    by_svc: dict[str, dict] = {}
    for fname_hash, methods in raw.items():
        fname = fname_hash.split(":")[0]
        for java_file in _TARGETS_DIR.rglob(fname):
            parts = java_file.parts
            svc = next((p for p in parts if p.startswith("ts-") and p.endswith("-service")), None)
            if svc:
                by_svc.setdefault(svc, {}).update(methods)
            break
    return by_svc


def _service_context(svc: str, passports: dict[str, dict]) -> str:
    """Краткий текстовый контекст сервиса для Qwen."""
    lines = [f"Service: {svc}"]
    methods = passports.get(svc, {})
    descs = [
        f"  {m}: {v.get('business_summary', '')}"
        for m, v in methods.items()
        if m != "home" and v.get("business_summary")
    ][:5]
    if descs:
        lines.append("Methods:")
        lines.extend(descs)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Структурные эвристики (fallback)
# ---------------------------------------------------------------------------

def _heuristic_g_side(a: str, b: str, reason: str) -> str:
    """Вернуть 'A' или 'B' — кто G-сторона по структурным признакам."""
    a_s = _strip(a)
    b_s = _strip(b)
    if reason.startswith("other_variant"):
        return "A" if not a_s.endswith("-other") else "B"
    if reason.startswith("numeric_variant"):
        return "A" if not (a_s and a_s[-1].isdigit()) else "B"
    # trigram: более короткое имя — G-сторона
    return "A" if len(a_s) <= len(b_s) else "B"


def _heuristic_basic_side(a: str, b: str, reason: str = "") -> str:
    """Вернуть 'A' или 'B' — кто basic-сторона для func_variant."""
    a_s = _strip(a)
    b_s = _strip(b)
    # admin_variant: non-admin = basic
    if reason == "admin_variant" or a_s.startswith("admin-") or b_s.startswith("admin-"):
        return "A" if not a_s.startswith("admin-") else "B"
    # func_variant: суффиксные сервисы (plan/delivery/price/food) = NOT basic
    _PLAN_SUFFIXES = ("-plan", "-delivery", "-price", "-food")
    a_has_suffix = any(a_s.endswith(s) for s in _PLAN_SUFFIXES)
    b_has_suffix = any(b_s.endswith(s) for s in _PLAN_SUFFIXES)
    if b_has_suffix and not a_has_suffix:
        return "A"
    if a_has_suffix and not b_has_suffix:
        return "B"
    return "A" if len(a_s) <= len(b_s) else "B"


# ---------------------------------------------------------------------------
# Qwen-классификация
# ---------------------------------------------------------------------------

def _qwen_classify_g_other(
    a: str, b: str, passports: dict, encoder
) -> tuple[str, float]:
    """Спросить Qwen кто PRIMARY-сторона (архитектурно-нейтральный промпт).
    Вернуть ('A'|'B', confidence) где 'A'/'B' — primary/standard сторона.
    """
    ctx_a = _service_context(a, passports)
    ctx_b = _service_context(b, passports)
    prompt = f"Service A:\n{ctx_a}\n\nService B:\n{ctx_b}\n\nJSON:"
    try:
        if encoder._backend == "ollama":
            raw = encoder._call_ollama(
                prompt, schema=_VARIANT_SCHEMA, system=_VARIANT_CLASSIFY_SYSTEM, num_predict=80
            )
        else:
            raw = {}
        primary = str(raw.get("primary_side", "")).upper().strip()
        conf = float(raw.get("confidence", 0.0))
        if primary in ("A", "B") and conf >= 0.6:
            return primary, conf
    except Exception as exc:
        print(f"  Qwen error: {exc}")
    return "", 0.0


def _qwen_classify_func(
    a: str, b: str, passports: dict, encoder
) -> tuple[str, float]:
    """Спросить Qwen кто core-сторона для func_variant (не используется, эвристика надёжнее)."""
    ctx_a = _service_context(a, passports)
    ctx_b = _service_context(b, passports)
    prompt = f"Service A:\n{ctx_a}\n\nService B:\n{ctx_b}\n\nJSON:"
    try:
        if encoder._backend == "ollama":
            raw = encoder._call_ollama(
                prompt, schema=_FUNC_SCHEMA, system=_FUNC_CLASSIFY_SYSTEM, num_predict=80
            )
        else:
            raw = {}
        core = str(raw.get("core_side", "")).upper().strip()
        conf = float(raw.get("confidence", 0.0))
        if core in ("A", "B") and conf >= 0.6:
            return core, conf
    except Exception as exc:
        print(f"  Qwen error: {exc}")
    return "", 0.0


# ---------------------------------------------------------------------------
# Определение типа пары
# ---------------------------------------------------------------------------

def _pair_type(reason: str) -> str:
    """Тип пары сиблингов по структурному признаку отношения."""
    if reason.startswith(("other_variant", "numeric_variant", "trigram")):
        return "primary_variant"
    if reason == "admin_variant":
        return "admin_variant"
    return "func_variant"


# ---------------------------------------------------------------------------
# Основная генерация
# ---------------------------------------------------------------------------

def generate_sibling_metadata(force: bool = False) -> dict:
    if CACHE_PATH.exists() and not force:
        existing = json.loads(CACHE_PATH.read_text())
        print(f"Загружен кеш: {len(existing)} записей")
        return existing

    skel = json.loads(SKEL_PATH.read_text())
    modules = sorted(n["name"] for n in skel["nodes"].values() if n.get("kind") == "module")
    siblings = detect_all_siblings(modules)  # [(a, b, score, reason)]
    print(f"Найдено пар сиблингов: {len(siblings)}")

    passports = _load_passports()
    print(f"Паспорта: {len(passports)} сервисов")

    from src.localization.qwen_encoder import QwenEncoder
    encoder = QwenEncoder()
    use_qwen = encoder.enabled
    if not use_qwen:
        print("Qwen недоступен — используем только эвристики")

    metadata: dict[str, dict] = {}

    for a, b, score, reason in siblings:
        ptype = _pair_type(reason)
        a_s = _strip(a)
        # variant_group: общая часть имени (без ts- и -service)
        stripped_a = a_s.replace("-other", "").rstrip("0123456789-")
        group = stripped_a

        print(f"\n[{a}] ↔ [{b}]  reason={reason!r}  type={ptype}")

        if ptype == "primary_variant":
            # other_variant (-other суффикс) — однозначный структурный сигнал,
            # не требует Qwen. Qwen для numeric_variant/trigram где имя менее очевидно.
            g_side_ab, conf = ("", 0.0)
            if use_qwen and not reason.startswith("other_variant"):
                primary_ab, conf = _qwen_classify_g_other(a, b, passports, encoder)
                g_side_ab = primary_ab
                print(f"  Qwen: primary_side={primary_ab!r}  conf={conf:.2f}")
            if not g_side_ab:
                g_side_ab = _heuristic_g_side(a, b, reason)
                conf = 1.0
                print(f"  Heuristic: primary_side={g_side_ab!r}")

            a_is_primary = (g_side_ab == "A")
            metadata[a] = {
                "variant_group": group,
                "variant_type": "primary_variant",
                "is_g_side": a_is_primary,
                "sibling": b,
                "confidence": conf,
            }
            metadata[b] = {
                "variant_group": group,
                "variant_type": "primary_variant",
                "is_g_side": not a_is_primary,
                "sibling": a,
                "confidence": conf,
            }

        elif ptype == "admin_variant":
            # Admin-сервисы записываются как отдельный тип — не смешиваем с func_variant.
            # Runtime не использует эти пары для disambiguate; тип сохраняется для наблюдаемости.
            a_s_name = _strip(a)
            a_is_admin = a_s_name.startswith("admin-")
            metadata[a] = {
                "variant_group": group,
                "variant_type": "admin_variant",
                "is_admin_side": a_is_admin,
                "sibling": b,
                "confidence": 1.0,
            }
            metadata[b] = {
                "variant_group": group,
                "variant_type": "admin_variant",
                "is_admin_side": not a_is_admin,
                "sibling": a,
                "confidence": 1.0,
            }
            print(f"  Admin pair — записано, runtime не использует")

        else:  # func_variant
            # Структурные эвристики по суффиксам (plan, delivery, price, food).
            # Qwen не используется — он путает "plan" (план маршрута) с базовым сервисом.
            basic_ab = _heuristic_basic_side(a, b, reason)
            conf = 1.0
            print(f"  Heuristic: basic_side={basic_ab!r}")

            a_is_basic = (basic_ab == "A")
            metadata[a] = {
                "variant_group": group,
                "variant_type": "func_variant",
                "is_basic_side": a_is_basic,
                "sibling": b,
                "confidence": conf,
            }
            metadata[b] = {
                "variant_group": group,
                "variant_type": "func_variant",
                "is_basic_side": not a_is_basic,
                "sibling": a,
                "confidence": conf,
            }

    CACHE_PATH.parent.mkdir(exist_ok=True)
    CACHE_PATH.write_text(json.dumps(metadata, indent=2, ensure_ascii=False))
    print(f"\nГотово: {len(metadata)} записей → {CACHE_PATH}")
    return metadata


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--force", action="store_true")
    args = p.parse_args()
    generate_sibling_metadata(force=args.force)
