"""AkShare news for A-shares.

Uses Eastmoney's per-stock news feed (``stock_news_em``), which returns recent
headlines with a date; it is current-only and does not archive, matching the
yfinance news vendor's behaviour.
"""

from __future__ import annotations

import logging

from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.vendors.akshare.common import (
    ak_retry,
    is_a_share,
    is_etf,
    normalize_ak_symbol,
)
from tradingagents.dataflows.vendors.akshare.etf_industry import (
    get_etf_holdings,
    get_etf_industry_keyword,
    get_etf_name,
)

logger = logging.getLogger(__name__)


def get_global_news(curr_date: str, look_back_days: int = 7, limit: int = 30) -> str:
    """Domestic (China) financial news from Caixin's main news feed.

    Mirrors ``get_global_news_yfinance`` so it drops into the ``get_global_news``
    vendor chain. Returns a formatted markdown block of recent Chinese financial
    headlines (title + summary + source) — a macro/market news read that the
    US-centric global-news vendors (yfinance/Alpha Vantage) do not serve for
    domestic markets.
    """
    import akshare as ak

    data = ak_retry(lambda: ak.stock_news_main_cx())

    if data is None or data.empty:
        raise VendorUnavailableError("Caixin returned no news")

    lines = ["## 国内财经新闻 (财新 Caixin):\n"]
    seen = 0
    for _, row in data.head(limit).iterrows():
        tag = str(row.get("tag") or "").strip()
        summary = str(row.get("summary") or "").strip()
        url = str(row.get("url") or "").strip()
        if not summary:
            continue
        head = f"### {summary}"
        if tag:
            head += f" [{tag}]"
        lines.append(head)
        if url:
            lines.append(f"来源: {url}")
        lines.append("")
        seen += 1

    if seen == 0:
        raise VendorUnavailableError("Caixin returned no usable news items")

    return "\n".join(lines).rstrip()


def get_news(
    ticker: str,
    start_date: str,
    end_date: str,
) -> str:
    """Recent news about an A-share ``ticker`` from Eastmoney.

    Returns a formatted markdown block of headlines. As with yfinance, this is
    a recent-only feed, so a historical window returns a clear unavailable
    signal (letting the router fall through) rather than fabricated content.

    For an ETF, the fund wrapper itself has little news; what matters is the
    *sector it tracks*. So an ETF call returns both the ETF's own news (fund
    flows, NAV records) and the news of its top constituent stocks — the
    semiconductor-equipment companies for a 科创半导体ETF, for example — so the
    analyst reads the underlying industry, not just the fund.
    """
    import akshare as ak

    if is_etf(ticker):
        return _get_etf_news(ticker)
    if not is_a_share(ticker):
        raise NoMarketDataError(
            ticker, ticker, f"'{ticker}' is not a recognized A-share code"
        )
    code = normalize_ak_symbol(ticker)

    data = ak_retry(lambda: ak.stock_news_em(symbol=code))

    if data is None or data.empty:
        raise VendorUnavailableError(f"Eastmoney returned no news for {code}")

    rename = {
        "新闻标题": "标题",
        "新闻内容": "内容",
        "发布时间": "时间",
        "文章来源": "来源",
        "新闻链接": "链接",
    }
    data = data.rename(columns=rename)
    cols = [c for c in ("标题", "内容", "时间", "来源", "链接") if c in data.columns]

    lines = [f"## {code} 新闻 (Eastmoney):\n"]
    for _, row in data.head(30).iterrows():
        title = str(row.get("标题") or "").strip()
        if not title:
            continue
        when = str(row.get("时间") or "").strip()
        source = str(row.get("来源") or "").strip()
        head = f"### {title}"
        if source:
            head += f" (source: {source})"
        lines.append(head)
        if when:
            lines.append(f"Published: {when}")
        content = str(row.get("内容") or "").strip()
        if content and content != title:
            lines.append(content[:300])
        lines.append("")

    return "\n".join(lines).rstrip()


def _get_etf_news(ticker: str) -> str:
    """News for an ETF: its own news plus the news of its top holdings.

    The holdings' headlines are the "industry news" for the sector the ETF
    tracks. Everything degrades gracefully; if nothing at all is available we
    raise VendorUnavailableError so the router can fall through.
    """
    import akshare as ak

    code = normalize_ak_symbol(ticker)
    name = get_etf_name(code) or code
    industry = get_etf_industry_keyword(code)

    lines = [f"## {name} ({code}) 新闻 (Eastmoney):"]
    if industry:
        lines.append(f"跟踪行业: {industry}")

    # --- 1) The ETF's own news (fund flows / NAV / listing records) ---
    own = None
    try:
        own = ak_retry(lambda: ak.stock_news_em(symbol=code))
    except VendorUnavailableError as e:
        logger.warning("ETF own news unavailable for %s: %s", code, e)

    if own is not None and not own.empty:
        lines.append(f"\n**{name} 基金动态**")
        lines.extend(_render_headlines(own, limit=10))
    else:
        lines.append("\n**基金动态：无近期新闻**")

    # --- 2) News of the top constituent stocks = the tracked industry ---
    holdings = get_etf_holdings(code)
    if holdings:
        lines.append(f"\n**跟踪行业成分股新闻（{industry or '持仓重仓股'}）**")
        any_hold_news = False
        for hcode, hname in holdings:
            try:
                hdata = ak_retry(lambda: ak.stock_news_em(symbol=hcode))
            except VendorUnavailableError:
                continue
            if hdata is None or hdata.empty:
                continue
            any_hold_news = True
            label = f"{hname} ({hcode})" if hname else hcode
            lines.append(f"\n#### {label}")
            lines.extend(_render_headlines(hdata, limit=3))
        if not any_hold_news:
            lines.append("（成分股新闻暂不可用）")
    else:
        lines.append("\n**跟踪行业成分股新闻：持仓数据不可用**")

    if len(lines) == 1 and own is None:
        raise VendorUnavailableError(f"Eastmoney returned no news for ETF {code}")

    return "\n".join(lines).rstrip()


def _render_headlines(data, limit: int = 30) -> list[str]:
    """Render Eastmoney news rows into markdown headline lines."""
    rename = {
        "新闻标题": "标题",
        "新闻内容": "内容",
        "发布时间": "时间",
        "文章来源": "来源",
        "新闻链接": "链接",
    }
    data = data.rename(columns=rename)
    lines = []
    for _, row in data.head(limit).iterrows():
        title = str(row.get("标题") or "").strip()
        if not title:
            continue
        when = str(row.get("时间") or "").strip()
        source = str(row.get("来源") or "").strip()
        head = f"### {title}"
        if source:
            head += f" (source: {source})"
        lines.append(head)
        if when:
            lines.append(f"Published: {when}")
        content = str(row.get("内容") or "").strip()
        if content and content != title:
            lines.append(content[:300])
        lines.append("")
    return lines
