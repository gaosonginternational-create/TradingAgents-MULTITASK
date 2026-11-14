"""Prediction ledger: 反馈 → 验证 → 沉淀 → 再预测.

Every analysis can leave behind what it *expected* (a falsifiable claim, a
direction and a horizon). Later, this module checks each open claim against the
realized price move, marks it hit/miss, and the scorecard is injected into the
next prompt — so a wrong driver is demoted instead of repeated, and a right one
is carried forward with evidence.

Design notes:
- Append-only JSONL: a claim is never rewritten, only annotated with its
  outcome, so the history stays auditable.
- Verification is mechanical (close at ``as_of`` vs close at ``as_of+horizon``),
  not another LLM call: the point is to catch self-serving narratives.
- A claim with no tradeable price at either end is left open rather than
  guessed.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


def _ledger_path() -> str:
    from tradingagents.dataflows.vendors.ima import ledger_path

    return ledger_path()


def _read() -> list[dict]:
    path = _ledger_path()
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _write_all(entries: list[dict]) -> None:
    path = _ledger_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for rec in entries:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def append_claim(
    ticker: str,
    as_of: str,
    claim: str,
    direction: str = "up",
    horizon_days: int = 20,
    confidence: float | None = None,
    sources: list[str] | None = None,
) -> dict:
    """Record one falsifiable expectation (idempotent per ticker+date+claim)."""
    direction = (direction or "up").lower()
    if direction not in ("up", "down", "flat"):
        raise ValueError("direction must be up/down/flat")
    entries = _read()
    claim_id = f"{ticker}:{as_of}:{abs(hash(claim)) % 10**8}"
    for rec in entries:
        if rec.get("id") == claim_id:
            return rec
    rec = {
        "id": claim_id,
        "ticker": ticker,
        "as_of": as_of,
        "claim": claim,
        "direction": direction,
        "horizon_days": int(horizon_days),
        "confidence": confidence,
        "sources": sources or [],
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "verified_at": None,
        "realized_pct": None,
        "hit": None,
    }
    entries.append(rec)
    _write_all(entries)
    return rec


def _close_on(ticker: str, date: str) -> float | None:
    """Close price on/after ``date`` (A-share via akshare, else yfinance)."""
    try:
        from tradingagents.dataflows.vendors.akshare.common import is_a_share, normalize_ak_symbol

        if is_a_share(ticker):
            import akshare as ak

            code = normalize_ak_symbol(ticker)
            start = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y%m%d")
            end = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=10)).strftime("%Y%m%d")
            df = ak.stock_zh_a_hist(symbol=code, period="daily",
                                    start_date=start, end_date=end, adjust="qfq")
            if df is None or df.empty:
                return None
            df = df[df["日期"].astype(str) >= date]
            return float(df.iloc[0]["收盘"]) if len(df) else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("close lookup failed for %s via akshare: %s", ticker, exc)

    try:
        import yfinance as yf

        start = datetime.strptime(date, "%Y-%m-%d") - timedelta(days=7)
        end = datetime.strptime(date, "%Y-%m-%d") + timedelta(days=10)
        hist = yf.Ticker(ticker).history(start=start.strftime("%Y-%m-%d"),
                                         end=end.strftime("%Y-%m-%d"),
                                         auto_adjust=True, actions=False)
        if hist is None or hist.empty:
            return None
        hist = hist[hist.index.strftime("%Y-%m-%d") >= date]
        return float(hist.iloc[0]["Close"]) if len(hist) else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("close lookup failed for %s via yfinance: %s", ticker, exc)
    return None


def verify(today: str | None = None) -> dict:
    """Fill in outcomes for claims whose horizon has elapsed. Returns a summary."""
    today = today or datetime.now().strftime("%Y-%m-%d")
    entries = _read()
    changed = 0
    for rec in entries:
        if rec.get("hit") is not None:
            continue
        as_of = rec.get("as_of")
        horizon = int(rec.get("horizon_days") or 20)
        if not as_of:
            continue
        due = (datetime.strptime(as_of, "%Y-%m-%d").date() + timedelta(days=horizon)).isoformat()
        if due > today:
            continue
        start_px = _close_on(rec["ticker"], as_of)
        end_px = _close_on(rec["ticker"], due)
        if not start_px or not end_px:
            continue
        pct = (end_px / start_px - 1) * 100
        direction = rec.get("direction", "up")
        hit = (pct > 1) if direction == "up" else (pct < -1) if direction == "down" else abs(pct) <= 1
        rec["verified_at"] = datetime.now().isoformat(timespec="seconds")
        rec["realized_pct"] = round(pct, 2)
        rec["hit"] = bool(hit)
        changed += 1
    if changed:
        _write_all(entries)
    verified = [e for e in entries if e.get("hit") is not None]
    hits = sum(1 for e in verified if e["hit"])
    return {
        "newly_verified": changed,
        "total_verified": len(verified),
        "hits": hits,
        "hit_rate": round(hits / len(verified) * 100, 1) if verified else None,
        "open": len(entries) - len(verified),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="prediction ledger (verify / list)")
    ap.add_argument("--verify", action="store_true", help="verify claims whose horizon elapsed")
    ap.add_argument("--ticker", help="only show one ticker")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.verify:
        print(json.dumps(verify(), ensure_ascii=False))
        return 0

    entries = _read()
    if args.ticker:
        entries = [e for e in entries if e.get("ticker") == args.ticker]
    for e in entries:
        mark = "✓" if e.get("hit") else ("✗" if e.get("hit") is False else "⏳")
        print(f"{mark} {e['ticker']:8s} {e['as_of']} {e['direction']:4s} "
              f"{e.get('realized_pct')} {e['claim'][:60]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
