"""AkShare fundamentals for A-shares.

AkShare exposes A-share fundamentals through a few endpoints; the most stable
for a per-ticker overview is the Eastmoney "financial analysis indicator"
table (``stock_financial_analysis_indicator``), which gives per-report-period
rows of valuation and quality metrics (PE/PB, ROE, margins, growth, debt).

The function returns a compact overview CSV plus the latest key ratios, styled
like ``yahoo/fundamentals.py::get_fundamentals`` so the LLM analyst reads it the
same way.
"""

from __future__ import annotations

import logging

import pandas as pd

from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.vendors.akshare.common import (
    ak_retry,
    is_a_share,
    normalize_ak_symbol,
    raise_for_empty,
)

logger = logging.getLogger(__name__)


def get_fundamentals(
    ticker: str,
    as_of_date: str | None = None,
) -> str:
    """Company fundamentals (valuation + quality) for an A-share ``ticker``.

    ``ticker`` is a 6-digit A-share code (e.g. ``600519``). ``as_of_date`` is
    accepted for signature parity but AkShare's indicator table is a current
    snapshot, so historical point-in-time fundamentals are not available here.
    """
    import akshare as ak

    if not is_a_share(ticker):
        raise NoMarketDataError(
            ticker, ticker, f"'{ticker}' is not a recognized A-share code"
        )
    code = normalize_ak_symbol(ticker)

    # Primary: Eastmoney financial-analysis indicator (rich, per-period).
    data = ak_retry(lambda: ak.stock_financial_analysis_indicator(symbol=code, start_year="2020"))
    if data is not None and not data.empty:
        return _format_indicators(code, ticker, data)

    raise_for_empty(ticker, code, "fundamental indicators")


def _format_indicators(code: str, ticker: str, df) -> str:
    """Render the indicator table as an overview block plus a compact CSV.

    The table is oldest-first; the last row is the latest report period.
    """
    latest = df.iloc[-1]

    def val(*keys):
        for k in keys:
            if k in df.columns:
                v = latest.get(k)
                if v is not None and v != "" and not pd.isna(v):
                    return v
        return None

    def pct(*keys):
        """Render a percent-valued column as '12.34%'."""
        v = val(*keys)
        return None if v is None else f"{v}%"

    fields = [
        ("报告期", val("日期")),
        ("每股收益 EPS", val("加权每股收益(元)", "摊薄每股收益(元)")),
        ("每股净资产", val("每股净资产_调整前(元)", "每股净资产_调整后(元)")),
        ("净资产收益率 ROE", pct("加权净资产收益率(%)", "净资产收益率(%)")),
        ("销售毛利率", pct("销售毛利率(%)")),
        ("销售净利率", pct("销售净利率(%)")),
        ("资产负债率", pct("资产负债率(%)")),
        ("流动比率", val("流动比率")),
        ("速动比率", val("速动比率")),
        ("营业收入增长率", pct("主营业务收入增长率(%)")),
        ("净利润增长率", pct("净利润增长率(%)")),
    ]
    lines = [f"{label}: {v}" for label, v in fields if v is not None and v != ""]

    if not lines:
        raise NoMarketDataError(ticker, code, "no fundamental fields")

    header = f"# Company Fundamentals for {code} (from {ticker})\n\n"
    overview = "\n".join(lines)

    # Cap the CSV at the latest 8 periods (oldest-first, so tail(8)) to keep
    # the prompt bounded while showing the recent trend.
    csv = df.tail(8).to_csv(index=False)
    return f"{header}{overview}\n\n## Recent period indicators (newest at bottom)\n\n{csv}"
