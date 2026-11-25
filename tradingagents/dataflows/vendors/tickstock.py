"""Local Tick Stock Panel (Quant Terminal) as a data vendor.

The user runs a Tick Stock Panel on the host that has already ingested the whole
A-share market: daily and minute bars, plus a wide set of precomputed
indicators (MA/EMA/MACD/BOLL/KDJ/ATR/RSI/momentum/turnover/limit prices and
signal flags). It answers on ``http://host.docker.internal:3018/api`` with no
authentication, and reading it is a local call — unlike Eastmoney's push2, which
throttles our IP after a burst of quote requests.

So this vendor sits at the front of the A-share quote chain: panel first,
Eastmoney/Sina as fallback when the panel is not running (the user may close it)
or when the symbol is not a domestic one (the panel only holds A-shares).

Only ``get_stock_data`` is served here. Everything downstream — the verified
market snapshot, the indicator tools, the charts — is derived from that same
OHLCV series, so one honest source is enough.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.vendors.akshare.common import is_a_share, is_etf

logger = logging.getLogger(__name__)

_DEFAULT_BASE = "http://host.docker.internal:3018"
_TIMEOUT = 25


def base_url() -> str:
    """Panel base URL; override with ``TICKSTOCK_BASE_URL`` when it moves."""
    return os.getenv("TICKSTOCK_BASE_URL", _DEFAULT_BASE).rstrip("/")


def panel_symbol(ticker: str) -> str:
    """``600999`` -> ``600999.SH``; ``000338`` -> ``000338.SZ``; ``830799`` -> ``830799.BJ``."""
    raw = (ticker or "").strip().upper()
    if "." in raw:
        code, _, suffix = raw.partition(".")
        suffix = suffix.upper()
        if suffix in ("SH", "SZ", "BJ"):
            return f"{code}.{suffix}"
        raw = code
    code = "".join(ch for ch in raw if ch.isdigit())
    if len(code) != 6:
        raise NoMarketDataError(ticker, ticker, f"'{ticker}' is not a 6-digit A-share code")
    head = code[0]
    if head == "6":                     # 沪市主板/科创板
        return f"{code}.SH"
    if head in ("0", "3"):              # 深市主板/创业板
        return f"{code}.SZ"
    if head == "5":                     # 沪市 ETF/LOF（588170 科创半导体ETF）
        return f"{code}.SH"
    if head in ("1", "2"):              # 深市基金（159852 软件ETF）
        return f"{code}.SZ"
    # 4/8 开头是北交所
    return f"{code}.BJ"


def _get(path: str, params: dict) -> dict | list:
    url = f"{base_url()}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        # 4xx means the panel answered but refused this request: a data problem,
        # not a transport problem.
        if 400 <= exc.code < 500:
            raise NoMarketDataError(
                str(params.get("symbol", "")), str(params.get("symbol", "")),
                f"panel returned HTTP {exc.code} for {path}",
            ) from exc
        raise VendorUnavailableError(f"Tick Stock Panel HTTP {exc.code} on {path}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise VendorUnavailableError(
            f"Tick Stock Panel unreachable at {base_url()} ({type(exc).__name__}); "
            "is the panel running?"
        ) from exc


def _daily_rows(symbol: str, start_date: str, end_date: str) -> list[dict]:
    """Raw daily bars from the panel, oldest first, clipped to the window."""
    panel_sym = panel_symbol(symbol)
    payload = _get("/api/kline/daily", {
        "symbol": panel_sym,
        "start_date": start_date,
        "end_date": end_date,
    })
    rows = payload if isinstance(payload, list) else (payload.get("rows") or [])
    out = []
    for r in rows:
        date = (r.get("date") or "")[:10]
        if date and start_date <= date <= end_date:
            out.append(r)
    if not out:
        raise NoMarketDataError(
            symbol, panel_sym,
            f"panel has no daily bars for {panel_sym} between {start_date} and {end_date}",
        )
    return out


def daily_frame(symbol: str, start_date: str, end_date: str):
    """Daily bars as a Date-indexed DataFrame (the shape ``load_ohlcv`` wants)."""
    import pandas as pd

    rows = _daily_rows(symbol, start_date, end_date)
    df = pd.DataFrame([{
        "Date": r.get("date")[:10],
        "Open": r.get("open"),
        "High": r.get("high"),
        "Low": r.get("low"),
        "Close": r.get("close"),
        "Volume": r.get("volume"),
        "Amount": r.get("amount"),
        "Turnover": r.get("turnover_rate"),
    } for r in rows])
    df["Date"] = pd.to_datetime(df["Date"])
    return df.sort_values("Date").reset_index(drop=True)


def get_stock(symbol: str, start_date: str, end_date: str) -> str:
    """Daily OHLCV for an A-share/ETF, in the same CSV shape the pipeline expects.

    Dates are inclusive and validated up front; the panel returns rows oldest
    first, which is what the rest of the pipeline assumes.
    """
    datetime.strptime(start_date, "%Y-%m-%d")
    datetime.strptime(end_date, "%Y-%m-%d")

    if not (is_a_share(symbol) or is_etf(symbol)):
        # Not a domestic symbol: let the router fall through to yfinance rather
        # than pretend the panel has it.
        raise NoMarketDataError(
            symbol, symbol,
            f"'{symbol}' is not an A-share/ETF; the Tick Stock Panel only holds the A-share market",
        )

    panel_sym = panel_symbol(symbol)
    rows = _daily_rows(symbol, start_date, end_date)

    # The panel reports change_pct as a fraction (-0.0263) while the pipeline's
    # PctChange column is percent (-2.63), as AkShare reports it.
    def _pct(v):
        return "" if v is None else f"{float(v) * 100:.4f}"

    def _num(v):
        return "" if v is None else v

    lines = ["Date,Open,Close,High,Low,Volume,Amount,PctChange,Change,Turnover"]
    for r in rows:
        date = (r.get("date") or "")[:10]
        if not date or date < start_date or date > end_date:
            continue
        lines.append(",".join([
            date,
            str(_num(r.get("open"))), str(_num(r.get("close"))),
            str(_num(r.get("high"))), str(_num(r.get("low"))),
            str(_num(r.get("volume"))), str(_num(r.get("amount"))),
            _pct(r.get("change_pct")), str(_num(r.get("change_amount"))),
            str(_num(r.get("turnover_rate"))),
        ]))

    body = "\n".join(lines[1:])
    if not body:
        raise NoMarketDataError(
            symbol, panel_sym,
            f"panel returned bars outside {start_date}..{end_date} for {panel_sym}",
        )

    label = f"{panel_sym} (from {symbol})" if panel_sym.split(".")[0] != symbol.upper() else panel_sym
    header = f"# Stock data for {label} from {start_date} to {end_date}\n"
    header += f"# Total records: {len(body.splitlines())}\n"
    header += f"# Source: Tick Stock Panel ({base_url()})\n\n"
    return header + lines[0] + "\n" + body + "\n"


def market_snapshot() -> list[dict]:
    """The whole A-share snapshot in one call (~5k rows, with caps/turnover)."""
    payload = _get("/api/screener/market-snapshot", {})
    rows = payload if isinstance(payload, list) else (payload.get("rows") or [])
    return rows
