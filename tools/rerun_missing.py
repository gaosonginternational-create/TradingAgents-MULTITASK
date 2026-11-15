"""Re-run tickers one at a time through the webapp API.

Each analysis container takes several GB; running four in parallel is what
killed the previous batch (OOM, SIGKILL, logs that just stop). This helper
submits one ticker, waits for it to finish, then submits the next, so memory
pressure stays bounded even though the webapp itself may not cap concurrency.

Usage:
    python tools/rerun_missing.py 002371 300308 588170 600999 GLW --date 2026-11-14
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

API = "http://127.0.0.1:8765"


def _get(path: str) -> dict:
    with urllib.request.urlopen(f"{API}{path}", timeout=15) as resp:
        return json.loads(resp.read())


def _post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def _status(task_id: str) -> str:
    for t in _get("/api/tasks").get("tasks", []):
        if t.get("id") == task_id:
            return t.get("status", "?")
    return "?"


def run_one(ticker: str, date: str, analysts: str, timeout_min: int = 45) -> str:
    resp = _post("/api/analyze", {"ticker": ticker, "date": date, "analysts": analysts})
    task_id = resp.get("task_id")
    if not task_id:
        return f"{ticker}: 提交失败 {resp}"
    print(f"  → {ticker} 已提交（task {task_id}），等待完成…", flush=True)
    deadline = time.time() + timeout_min * 60
    last = None
    while time.time() < deadline:
        st = _status(task_id)
        if st != last:
            print(f"    [{time.strftime('%H:%M:%S')}] {ticker}: {st}", flush=True)
            last = st
        if st in ("done", "error", "cancelled"):
            return f"{ticker}: {st}"
        time.sleep(20)
    return f"{ticker}: 超时未结束（{timeout_min} 分钟）"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tickers", nargs="+")
    ap.add_argument("--date", required=True)
    ap.add_argument("--analysts", default="market,sentiment,news,fundamentals")
    args = ap.parse_args(argv)

    results = []
    for i, ticker in enumerate(args.tickers, 1):
        print(f"[{i}/{len(args.tickers)}] {ticker}", flush=True)
        try:
            results.append(run_one(ticker, args.date, args.analysts))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            results.append(f"{ticker}: 请求异常 {exc}")
        # 让上一个容器的内存彻底释放，再开下一个。
        time.sleep(10)

    print("\n结果汇总：", flush=True)
    for line in results:
        print("  " + line, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
