#!/usr/bin/env python3
"""Отправка submission на платформу olympic.nsu.ru и polling результата.

Использование:
    python scripts/submit.py                   # последний submission_*.zip
    python scripts/submit.py submission.zip    # конкретный файл
    python scripts/submit.py --scores          # только показать последние результаты
    python scripts/submit.py --no-poll ...     # без ожидания

Cookie берётся из .env файла в корне проекта:
    CGISESSID=xxxxxxxxxxxxx
    __ddg1_=xxxxx
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("Нужен requests: pip install requests")

ROOT = Path(__file__).parent.parent

SUBMIT_URL   = "https://olympic.nsu.ru/nsuts-new/api/submit/do_submit"
REPORT_URL   = "https://olympic.nsu.ru/nsuts-new/api/report/get_report"
TASK_ID      = "124295"
LANG         = "zip"

POLL_INTERVAL = 8    # секунд между проверками
POLL_TIMEOUT  = 300  # максимум ждать


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def build_cookie(env: dict[str, str], extra: str | None) -> str:
    parts: list[str] = []
    if extra:
        parts.append(extra.strip())
    if "CGISESSID" in env and "CGISESSID" not in (extra or ""):
        parts.append(f"CGISESSID={env['CGISESSID']}")
    if "__ddg1_" in env and "__ddg1_" not in (extra or ""):
        parts.append(f"__ddg1_={env['__ddg1_']}")
    return "; ".join(parts)


def find_latest_zip() -> Path:
    zips = sorted(ROOT.glob("submission_*.zip"), key=lambda p: p.stat().st_mtime)
    if not zips:
        sys.exit("Нет submission_*.zip в корне проекта")
    return zips[-1]


def make_headers(cookie: str) -> dict:
    return {
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "User-Agent":      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:158.0) Gecko/20100101 Firefox/158.0",
        "Origin":          "https://olympic.nsu.ru",
        "Referer":         "https://olympic.nsu.ru/nsuts-new/submit",
        "Cookie":          cookie,
    }


def submit(zip_path: Path, cookie: str) -> None:
    print(f"Отправляю: {zip_path.name} ({zip_path.stat().st_size // 1024} КБ)")
    with zip_path.open("rb") as fh:
        resp = requests.post(
            SUBMIT_URL,
            headers=make_headers(cookie),
            data={"langId": LANG, "taskId": TASK_ID, "sourceText": ""},
            files={"sourceFile": (zip_path.name, fh, "application/zip")},
            timeout=120,
        )
    resp.raise_for_status()
    print(f"Ответ: {resp.text}")


def get_submits(cookie: str) -> list[dict]:
    resp = requests.get(
        REPORT_URL,
        headers=make_headers(cookie),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("submits", [])


def parse_points(points_str: str) -> list[float]:
    try:
        return [float(x) for x in str(points_str).split()]
    except Exception:
        return []


def print_scores(submits: list[dict], n: int = 5) -> None:
    print(f"\n{'id':>10}  {'дата':>20}  {'total':>8}  S1-S7")
    print("-" * 90)
    for s in submits[:n]:
        sid  = s.get("id", "?")
        date = s.get("date", "?")
        total = s.get("total", "?")
        pts  = parse_points(s.get("points", ""))
        if pts:
            avg  = sum(pts) / len(pts)
            pts_str = "  ".join(f"{p:.3f}" for p in pts)
            print(f"{sid:>10}  {date:>20}  {avg:.6f}  {pts_str}")
        else:
            status = s.get("status", "?")
            print(f"{sid:>10}  {date:>20}  {total:>8}  status={status}")


def poll_result(cookie: str, before_id: str | None) -> None:
    print(f"\nОжидаю нового результата (каждые {POLL_INTERVAL}с)...")
    deadline = time.time() + POLL_TIMEOUT

    while time.time() < deadline:
        time.sleep(POLL_INTERVAL)
        try:
            submits = get_submits(cookie)
        except Exception as exc:
            print(f"  Ошибка опроса: {exc}")
            continue

        if not submits:
            continue

        latest = submits[0]
        lid    = latest.get("id")
        pts    = parse_points(latest.get("points", ""))

        # Новый результат появился
        if lid != before_id and pts:
            avg = sum(pts) / len(pts)
            pts_str = "  ".join(f"{p:.3f}" for p in pts)
            print(f"\nНОВЫЙ РЕЗУЛЬТАТ (id={lid}):")
            print(f"  S1-S7: {pts_str}")
            print(f"  avg:   {avg:.6f}")
            return
        elapsed = int(POLL_TIMEOUT - (deadline - time.time()))
        print(f"  [{elapsed:3d}с] ждём (последний id={lid})...", end="\r")

    print("\nТаймаут ожидания результата")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("zip", nargs="?", help="путь к ZIP-файлу (по умолч. — последний submission_*.zip)")
    ap.add_argument("--cookie", default=None, help='Cookie строка явно: "CGISESSID=xxx"')
    ap.add_argument("--no-poll", action="store_true", help="не ждать результата")
    ap.add_argument("--scores", action="store_true", help="только показать последние результаты, не отправлять")
    ap.add_argument("-n", type=int, default=5, help="количество последних результатов (--scores)")
    a = ap.parse_args()

    env = load_env(ROOT / ".env")
    cookie = build_cookie(env, a.cookie)
    if not cookie:
        sys.exit("Нет Cookie. Добавьте CGISESSID=... в .env или передайте --cookie 'CGISESSID=...'")

    if a.scores:
        submits = get_submits(cookie)
        print_scores(submits, n=a.n)
        return

    zip_path = Path(a.zip) if a.zip else find_latest_zip()
    if not zip_path.exists():
        sys.exit(f"Файл не найден: {zip_path}")

    # Запомнить id последнего submission до отправки
    try:
        submits_before = get_submits(cookie)
        before_id = submits_before[0].get("id") if submits_before else None
    except Exception:
        before_id = None

    submit(zip_path, cookie)

    if not a.no_poll:
        poll_result(cookie, before_id)
    else:
        print("\nРезультаты (последние 3):")
        try:
            print_scores(get_submits(cookie), n=3)
        except Exception:
            pass


if __name__ == "__main__":
    main()
