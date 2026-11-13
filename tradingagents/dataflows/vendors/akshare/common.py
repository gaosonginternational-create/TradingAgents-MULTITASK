"""Shared helpers for the AkShare vendors: symbol parsing and error mapping."""

from __future__ import annotations

import json
import logging
import os
import re

from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError

logger = logging.getLogger(__name__)


# A-share code: 6 digits. Market is inferred from the leading digits.
#   Shanghai: 600/601/603/605/688 (STAR market)
#   Shenzhen: 000/001/002/003/300/301 (300/301 = ChiNext)
#   Beijing:  8xx/4xx (BSE) — supported for quotes, spotty for fundamentals.
_SH_A = re.compile(r"^(60[0135]|68[089])\d{3}$")
_SZ_A = re.compile(r"^(00[0123]|30[01])\d{3}$")
_BJ_A = re.compile(r"^(4|8)\d{5}$")

# Exchange-traded funds (场内基金/ETF/LOF), 6 digits. These are NOT stocks —
# they have no financial statements, and their quotes must come from
# fund_etf_hist_em rather than stock_zh_a_hist.
#   Shanghai ETF: 51x / 52x / 53x / 56x / 58x (incl. 588/589 STAR-market ETF)
#   Shenzhen ETF/LOF: 15x / 16x / 18x
_ETF_SH = re.compile(r"^5[12368]\d{4}$")
_ETF_SZ = re.compile(r"^1[568]\d{4}$")

# HK code as the user types it: bare digits, or digits + ".HK".
_HK = re.compile(r"^(\d{1,5})(?:\.HK)?$")

# Yahoo-style A-share suffixes that leak in from the generic normalizer:
# 600519.SS -> 600519, 000001.SZ -> 000001.
_YAHOO_SUFFIX = re.compile(r"^(\d{6})\.(SS|SZ)$")


def is_a_share(symbol: str) -> bool:
    """True for a Shanghai/Shenzhen/Beijing A-share code (6 digits, no suffix)."""
    if not isinstance(symbol, str):
        return False
    s = symbol.strip().upper()
    s = _YAHOO_SUFFIX.sub(r"\1", s)
    return bool(_SH_A.match(s) or _SZ_A.match(s) or _BJ_A.match(s))


def is_etf(symbol: str) -> bool:
    """True for an exchange-traded fund / LOF code (6 digits, no suffix).

    ETFs trade like stocks on the exchange but have no fundamentals; quotes
    must use ``fund_etf_hist_em`` instead of ``stock_zh_a_hist``.
    """
    if not isinstance(symbol, str):
        return False
    s = symbol.strip().upper()
    s = _YAHOO_SUFFIX.sub(r"\1", s)
    return bool(_ETF_SH.match(s) or _ETF_SZ.match(s))


def is_cn_market(symbol: str) -> bool:
    """True for any Chinese-market security we can serve via AkShare:
    A-shares, HK equities, or exchange-traded funds (ETF/LOF)."""
    return is_a_share(symbol) or is_hk(symbol) or is_etf(symbol)


def is_hk(symbol: str) -> bool:
    """True for a Hong Kong code (bare digits or digits + .HK)."""
    if not isinstance(symbol, str):
        return False
    s = symbol.strip().upper()
    return bool(_HK.match(s))


def normalize_ak_symbol(symbol: str) -> str:
    """Return the bare AkShare code for an A-share/HK symbol.

    Strips Yahoo suffixes (``600519.SS`` -> ``600519``) and HK zero-pads
    (``700.HK`` -> ``00700``, AkShare's 5-digit HK convention).
    """
    s = symbol.strip().upper()
    s = _YAHOO_SUFFIX.sub(r"\1", s)
    m = _HK.match(s)
    if m:
        return f"{int(m.group(1)):05d}"
    return s


def ak_retry(fn):
    """Run ``fn``; translate upstream failures into a typed vendor error.

    AkShare has no SLA and its sources occasionally change, so a transient
    failure must read as "vendor unavailable" (so the router can try the next
    vendor) rather than crashing the analysis.
    """
    try:
        return fn()
    except VendorUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001 - akshare surfaces many error types
        raise VendorUnavailableError(f"AkShare request failed: {exc}") from exc


# --- Eastmoney push2 quote rate-limit circuit breaker ---------------------
# push2 (the quote server behind stock_zh_a_hist / stock_hk_hist /
# fund_etf_hist_em) throttles an IP that calls too often. Its own error text
# says repeated calls *while throttled* extend the ban — so the worst thing a
# nightly batch can do is keep hammering it ticker after ticker. Once a push2
# rate limit is seen, we trip a process-wide cooldown and fail quote calls fast
# for a few minutes instead of adding to the ban. Statement/fundamental
# endpoints live on a different Eastmoney host and are unaffected, so the
# cooldown is applied only to the quote paths (see ``ak_quote_retry``).
PUSH2_COOLDOWN_SECONDS = 300.0
_push2_cooldown_until = 0.0
_PUSH2_COOLDOWN_FILE = "push2_cooldown.json"


def _is_push2_rate_limit(exc: BaseException) -> bool:
    """Whether ``exc`` is Eastmoney push2's rate-limit signal."""
    name = type(exc).__name__
    msg = str(exc)
    if "push2" in name.lower():
        return True
    low = msg.lower()
    return "push2" in low or "限流" in msg


def _cooldown_file_path():
    """Path of the cross-process cooldown marker, or None if unavailable.

    The nightly batch runs one container per ticker, so an in-process cooldown
    dies with each container. The marker lives in the shared, mounted cache dir
    (``data_cache_dir``) so a rate limit seen by one ticker's run pauses quote
    calls for the rest of the batch instead of letting 23 containers each add to
    the ban. Any failure here is silent — a cooldown is an optimisation, never a
    reason to break a run.
    """
    try:
        from tradingagents.dataflows.config import get_config

        cache_dir = get_config().get("data_cache_dir")
        if cache_dir:
            return os.path.join(cache_dir, _PUSH2_COOLDOWN_FILE)
    except Exception:  # noqa: BLE001
        pass
    return None


def _read_cooldown_file() -> float:
    """Seconds left on the shared cooldown marker (0 when absent/expired)."""
    import time

    path = _cooldown_file_path()
    if not path:
        return 0.0
    try:
        with open(path, encoding="utf-8") as fh:
            until = float(json.load(fh).get("until_epoch", 0))
        return max(0.0, until - time.time())
    except Exception:  # noqa: BLE001 - missing/corrupt marker just means no cooldown
        return 0.0


def _write_cooldown_file(seconds: float) -> None:
    """Record a cooldown for other processes; best effort, never raises."""
    import time

    path = _cooldown_file_path()
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"until_epoch": time.time() + seconds, "seconds": seconds}, fh)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        pass


def push2_cooldown_remaining() -> float:
    """Seconds left on the push2 cooldown (max of in-process and shared marker)."""
    import time

    return max(max(0.0, _push2_cooldown_until - time.monotonic()), _read_cooldown_file())


def ak_quote_retry(fn):
    """Like ``ak_retry`` but trips a cooldown on an Eastmoney push2 rate limit.

    Use this around *quote* calls (daily OHLCV). On a push2 rate limit it raises
    ``VendorUnavailableError`` with a clear "cooling down" message and refuses
    further quote calls for ``PUSH2_COOLDOWN_SECONDS`` — in this process and, via
    a marker in the shared cache dir, in the other containers of a batch — so a
    rate limit cannot be made worse by repeated calls. Other failures degrade
    exactly like ``ak_retry``.
    """
    import time

    global _push2_cooldown_until
    remaining = push2_cooldown_remaining()
    if remaining > 0:
        raise VendorUnavailableError(
            f"Eastmoney push2 is rate-limited; quote calls are paused for another "
            f"{remaining:.0f}s to let the limit reset. Do not retry immediately."
        )
    try:
        return fn()
    except VendorUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001 - akshare surfaces many error types
        if _is_push2_rate_limit(exc):
            _push2_cooldown_until = time.monotonic() + PUSH2_COOLDOWN_SECONDS
            _write_cooldown_file(PUSH2_COOLDOWN_SECONDS)
            raise VendorUnavailableError(
                f"Eastmoney push2 rate limit hit ({exc}). Quote calls are paused for "
                f"{PUSH2_COOLDOWN_SECONDS:.0f}s to let the limit reset; do not retry "
                f"immediately or the ban may lengthen."
            ) from exc
        raise VendorUnavailableError(f"AkShare request failed: {exc}") from exc


# --- Daily OHLCV with a different-host fallback ---------------------------
# push2 throttles an IP, not a symbol. Sina serves the same A-share daily bars
# from a different host, so when push2 is rate-limited (or in cooldown) the run
# can still get real prices instead of failing — this is what keeps a nightly
# batch productive during an Eastmoney ban.
_SINA_PREFIX = {"sh": ("60", "68", "9"), "sz": ("00", "30", "8", "4")}


def _sina_symbol(code: str) -> str:
    """``603986`` -> ``sh603986``; ``000001`` -> ``sz000001`` (Sina's convention)."""
    for prefix, heads in _SINA_PREFIX.items():
        if code.startswith(heads):
            return f"{prefix}{code}"
    return f"sz{code}"


def _normalize_cn_frame(df, volume_in_shares: bool = False):
    """Rename a push2 (Chinese) or Sina (lowercase) OHLCV frame to the canonical columns.

    Sina reports ``volume`` in shares while push2's 成交量 is in lots (手, 100
    shares); pass ``volume_in_shares=True`` to divide by 100 so indicator inputs
    stay on the same scale whichever host served the bars.
    """
    import pandas as pd

    if df is None or getattr(df, "empty", True):
        return df
    rename = {
        "日期": "Date", "开盘": "Open", "收盘": "Close", "最高": "High",
        "最低": "Low", "成交量": "Volume", "成交额": "Amount", "换手率": "Turnover",
        "date": "Date", "open": "Open", "close": "Close", "high": "High",
        "low": "Low", "volume": "Volume", "amount": "Amount", "turnover": "Turnover",
    }
    df = df.rename(columns=rename)
    keep = [c for c in ("Date", "Open", "Close", "High", "Low", "Volume", "Amount", "Turnover")
            if c in df.columns]
    df = df[keep].copy()
    if "Volume" in df.columns:
        df["Volume"] = pd.to_numeric(df["Volume"], errors="coerce")
        if volume_in_shares:
            df["Volume"] = df["Volume"] / 100.0  # 股 -> 手, matching push2
    return df


def fetch_a_share_daily(code: str, start: str, end: str, adjust: str = "qfq"):
    """Daily OHLCV for an A-share ``code`` (6 digits), push2 first then Sina.

    ``start``/``end`` are YYYYMMDD. Returns a frame with canonical
    Date/Open/Close/High/Low/Volume columns (as much as the source gives), or
    raises ``VendorUnavailableError`` when both hosts fail. Sina is only used
    for A-shares; HK/ETF callers keep their own endpoints.
    """
    import akshare as ak

    # Primary: Eastmoney push2 (unless a recent rate limit put it in cooldown).
    if push2_cooldown_remaining() <= 0:
        try:
            raw = ak_quote_retry(lambda: ak.stock_zh_a_hist(
                symbol=code, period="daily", start_date=start, end_date=end, adjust=adjust))
            return _normalize_cn_frame(raw)
        except VendorUnavailableError as exc:
            logger.warning("push2 daily failed for %s, falling back to Sina: %s", code, exc)

    # Fallback: Sina, a different host, unaffected by an Eastmoney IP ban.
    try:
        raw = ak_retry(lambda: ak.stock_zh_a_daily(
            symbol=_sina_symbol(code), start_date=start, end_date=end, adjust=adjust))
    except VendorUnavailableError as exc:
        raise VendorUnavailableError(
            f"Both Eastmoney push2 and Sina daily quotes failed for {code}: {exc}"
        ) from exc
    return _normalize_cn_frame(raw, volume_in_shares=True)


def raise_for_empty(symbol: str, canonical: str, detail: str) -> None:
    raise NoMarketDataError(symbol, canonical, detail)
