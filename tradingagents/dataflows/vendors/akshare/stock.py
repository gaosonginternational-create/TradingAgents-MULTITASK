"""AkShare daily OHLCV for A-shares and HK equities."""

from __future__ import annotations

import logging
from datetime import datetime

from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.vendors.akshare.common import (
    ak_quote_retry,
    ak_retry,
    fetch_a_share_daily,
    is_a_share,
    is_etf,
    is_hk,
    normalize_ak_symbol,
    raise_for_empty,
)

logger = logging.getLogger(__name__)


def get_stock(
    symbol: str,
    start_date: str,
    end_date: str,
) -> str:
    """Daily OHLCV (qfq adjusted) for an A-share or HK code, as CSV.

    Mirrors ``yahoo/market.py::get_YFin_data_online`` so it drops into the
    ``get_stock_data`` vendor chain. ``symbol`` is the user's ticker
    (e.g. ``600519``, ``600519.SH``, ``0700.HK``); the market is inferred from
    the code.
    """
    import akshare as ak

    datetime.strptime(start_date, "%Y-%m-%d")
    datetime.strptime(end_date, "%Y-%m-%d")

    # AkShare wants YYYYMMDD.
    start = start_date.replace("-", "")
    end = end_date.replace("-", "")

    if is_hk(symbol):
        code = normalize_ak_symbol(symbol)  # e.g. 00700
        data = ak_quote_retry(
            lambda: ak.stock_hk_hist(
                symbol=code,
                period="daily",
                start_date=start,
                end_date=end,
                adjust="qfq",
            )
        )
    elif is_etf(symbol):
        # ETF/LOF: no fundamentals, quotes come from the fund endpoint.
        code = normalize_ak_symbol(symbol)  # 588170
        data = ak_quote_retry(
            lambda: ak.fund_etf_hist_em(
                symbol=code,
                period="daily",
                start_date=start,
                end_date=end,
                adjust="qfq",
            )
        )
    elif is_a_share(symbol):
        code = normalize_ak_symbol(symbol)  # 600519
        # push2 first, Sina fallback when Eastmoney rate-limits the IP.
        data = fetch_a_share_daily(code, start, end, adjust="qfq")
    else:
        # Not a Chinese-market symbol: signal "no data" so the router tries the
        # next vendor (yfinance etc.) rather than mis-pricing a US ticker.
        raise NoMarketDataError(
            symbol, symbol, f"'{symbol}' is not a recognized A-share, HK, or ETF code"
        )

    if data is None or data.empty:
        raise_for_empty(symbol, code, f"rows between {start_date} and {end_date}")

    # AkShare returns a Chinese-column frame. Rename to the OHLCV convention the
    # rest of the pipeline (and the LLM) expects, and keep only those columns.
    rename = {
        "日期": "Date",
        "开盘": "Open",
        "收盘": "Close",
        "最高": "High",
        "最低": "Low",
        "成交量": "Volume",
        "成交额": "Amount",
        "振幅": "Amplitude",
        "涨跌幅": "PctChange",
        "涨跌额": "Change",
        "换手率": "Turnover",
    }
    data = data.rename(columns=rename)
    keep = [c for c in ("Date", "Open", "Close", "High", "Low", "Volume", "Amount",
                        "PctChange", "Change", "Turnover") if c in data.columns]
    data = data[keep]

    label = f"{code} (from {symbol})" if code != symbol.upper() else code
    header = f"# Stock data for {label} from {start_date} to {end_date}\n"
    header += f"# Total records: {len(data)}\n\n"
    return header + data.to_csv(index=False)
