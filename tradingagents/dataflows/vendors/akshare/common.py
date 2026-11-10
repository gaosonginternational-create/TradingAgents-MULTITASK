"""Shared helpers for the AkShare vendors: symbol parsing and error mapping."""

from __future__ import annotations

import re

from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError


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


def raise_for_empty(symbol: str, canonical: str, detail: str) -> None:
    raise NoMarketDataError(symbol, canonical, detail)
