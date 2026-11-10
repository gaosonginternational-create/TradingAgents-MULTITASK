"""ETF industry/theme mapping for AkShare domestic vendors.

A-share ETFs (场内基金, 51x/52x/53x/56x/58x 沪、15x/16x/18x 深) have no
financial statements and are not in Eastmoney's 千股千评 (per-stock comment
score) board, so the stock-oriented news/sentiment vendors return nothing for
them. But an ETF *tracks* an index or a sector: 科创半导体ETF (588170) tracks
semiconductor equipment & materials; 医药ETF tracks pharma; 军工ETF tracks
defense. The right "news about this instrument" for an ETF is therefore news
about the *sector it tracks*, not about the fund wrapper.

This module resolves that mapping two ways, best-effort:

  1. **Live holdings** — ``fund_portfolio_hold_em`` returns the ETF's top
     constituent stocks (the actual companies the fund holds). Grabbing news /
     sentiment for the top few holdings is the most faithful "what is this ETF
     tracking" signal and needs no hand-maintained table.

  2. **Static keyword fallback** — a small built-in code→industry-keyword table
     for the common broad/sector ETFs, used when the holdings lookup fails or
     to give the analyst a human-readable theme label.

It also resolves the ETF's display name via ``fund_etf_spot_em`` so reports can
say "科创半导体ETF华夏" instead of the bare code.
"""

from __future__ import annotations

import logging

from tradingagents.dataflows.vendors.akshare.common import ak_retry, is_etf

logger = logging.getLogger(__name__)

# Static code → (display name, industry keyword) fallback for common ETFs.
# The industry keyword is what the news/sentiment vendors use to describe the
# sector this ETF tracks; the display name is for human-readable reports.
# Only consulted when the live holdings lookup fails or returns nothing.
ETF_INDUSTRY_MAP = {
    # 科创50 / 科创板
    "588000": ("科创50ETF", "科创50/科创板"),
    "588080": ("科创板50ETF", "科创50/科创板"),
    "588090": ("科创板ETF", "科创50/科创板"),
    "588170": ("科创半导体ETF", "半导体设备材料"),
    "588200": ("科创芯片ETF", "半导体/芯片"),
    "588400": ("科创信息技术ETF", "信息技术/半导体"),
    # 半导体 / 芯片
    "512480": ("半导体ETF", "半导体/芯片"),
    "512760": ("半导体芯片ETF", "半导体/芯片"),
    "159813": ("芯片ETF", "半导体/芯片"),
    "159995": ("半导体芯片ETF", "半导体/芯片"),
    # 医药 / 医疗
    "512010": ("医药ETF", "医药生物"),
    "512170": ("医疗ETF", "医疗器械/医疗服务"),
    "159929": ("医药ETF", "医药生物"),
    "512290": ("生物医药ETF", "医药生物"),
    # 军工
    "512660": ("军工ETF", "国防军工"),
    "512710": ("军工龙头ETF", "国防军工"),
    # 新能源 / 光伏 / 车
    "516160": ("新能源ETF", "新能源"),
    "515030": ("新能源车ETF", "新能源汽车"),
    "515790": ("光伏ETF", "光伏"),
    # 消费 / 酒
    "512690": ("酒ETF", "白酒/食品饮料"),
    "159928": ("消费ETF", "消费"),
    # 金融
    "510230": ("金融ETF", "银行/券商/保险"),
    "512880": ("证券ETF", "券商"),
    # 科技 / 互联网
    "515000": ("科技ETF", "科技"),
    "513050": ("中概互联ETF", "互联网/中概"),
    "159605": ("中概互联ETF", "互联网/中概"),
    # 有色 / 资源
    "512400": ("有色金属ETF", "有色金属"),
}

# How many top holdings to use as the ETF's "sector representative" stocks
# for news/sentiment aggregation.
TOP_HOLDINGS = 5


def get_etf_name(ticker: str) -> str:
    """Resolve an ETF's display name via ``fund_etf_spot_em``; empty on failure."""
    if not is_etf(ticker):
        return ""
    import akshare as ak

    try:
        spot = ak_retry(lambda: ak.fund_etf_spot_em())
        if spot is None or spot.empty:
            return ""
        hit = spot[spot["代码"].astype(str) == ticker]
        if not hit.empty:
            name = str(hit.iloc[0].get("名称") or "").strip()
            if name:
                return name
    except Exception as e:  # noqa: BLE001
        logger.warning("ETF name lookup failed for %s: %s", ticker, e)
    return ""


def get_etf_holdings(ticker: str) -> list[tuple[str, str]]:
    """Return the ETF's top constituent stocks as ``[(code, name), ...]``.

    Uses ``fund_portfolio_hold_em`` (latest reported quarter). Empty list on
    failure or if the ETF has no holdings feed. Each entry is a 6-digit A-share
    code and its display name — the companies whose news/sentiment represent the
    sector this ETF tracks.
    """
    if not is_etf(ticker):
        return []
    import akshare as ak

    try:
        hold = ak_retry(lambda: ak.fund_portfolio_hold_em(symbol=ticker, date="2025"))
        if hold is None or hold.empty:
            return []
        out = []
        for _, row in hold.head(TOP_HOLDINGS).iterrows():
            code = str(row.get("股票代码") or "").strip()
            name = str(row.get("股票名称") or "").strip()
            if code and code.isdigit() and len(code) == 6:
                out.append((code, name))
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("ETF holdings lookup failed for %s: %s", ticker, e)
        return []


def get_etf_industry_keyword(ticker: str) -> str:
    """Return a human-readable industry keyword for an ETF.

    Prefers the static map; falls back to a keyword derived from the display
    name (stripping the "ETF/华夏/华宝" wrappers). Empty string when unknown.
    """
    if not is_etf(ticker):
        return ""
    name, kw = ETF_INDUSTRY_MAP.get(ticker, ("", ""))
    if kw:
        return kw
    # Derive a rough keyword from the resolved name: "科创半导体ETF华夏" ->
    # "科创半导体". Strip the fund-company suffix and the trailing "ETF/LOF".
    name = name or get_etf_name(ticker)
    if not name:
        return ""
    import re

    name = re.sub(r"(ETF|LOF|基金)$", "", name)
    name = re.sub(r"(华夏|华宝|易方达|南方|广发|嘉实|富国|博时|招商|国泰|汇添富|工银|银华|天弘)$", "", name)
    name = name.strip()
    return name or ""
