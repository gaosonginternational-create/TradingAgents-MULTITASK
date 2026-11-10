"""AkShare domestic (A-share) retail sentiment from Eastmoney.

Domestic retail sentiment differs from US sources (StockTwits/Reddit/SentiSense):
there is no cashtag stream or model-scored polarity. Instead, Eastmoney's
"千股千评" (per-stock comment score) publishes a proprietary sentiment score,
attention index and popularity rank for every A-share — a ready-made,
machine-readable sentiment read. We combine that with the popularity board and
the hot-keyword feed to give the analyst a concrete domestic sentiment picture.

Note: Xueqiu (雪球) hot-tweet endpoints have been walled off by anti-scraping and
are NOT used; Eastmoney is the reliable primary source here.
"""

from __future__ import annotations

import logging

from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.vendors.akshare.common import (
    ak_retry,
    is_cn_market,
    is_etf,
    normalize_ak_symbol,
)
from tradingagents.dataflows.vendors.akshare.etf_industry import (
    get_etf_holdings,
    get_etf_industry_keyword,
    get_etf_name,
)

logger = logging.getLogger(__name__)


def _fmt_num(v):
    """Render a numeric cell compactly, skipping NaN/None."""
    if v is None:
        return None
    try:
        f = float(v)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return v


def get_cn_sentiment(ticker: str) -> str:
    """Eastmoney retail-sentiment snapshot for a Chinese-market ``ticker``.

    Returns a formatted markdown block with:
      - 千股千评: 综合得分 (composite score), 关注指数 (attention), 目前排名
        (popularity rank), 机构参与度 (institutional participation), 上升
        (attention trend)
      - 人气榜: the ticker's position on the 100-stock popularity board, if present
      - 热搜关键词: hot concepts linked to the ticker with heat values

    Degrades gracefully: any upstream failure becomes a clear unavailable signal
    rather than an exception, so the analyst always sees something.
    """
    import akshare as ak

    if not is_cn_market(ticker):
        raise NoMarketDataError(
            ticker, ticker, f"'{ticker}' is not a recognized A-share/HK/ETF code"
        )
    code = normalize_ak_symbol(ticker)  # 600519 (ETFs keep their 6 digits)

    # ETFs are not in 千股千评 / 人气榜, so route to the sector-aggregate read:
    # the sentiment of the top holdings (the sector the ETF tracks) plus the
    # ETF's own fund flow.
    if is_etf(ticker):
        return _get_etf_sentiment(ticker)

    # Eastmoney uses an exchange-prefixed code for keyword/rank lookups:
    # SH for Shanghai (6xx, 5xx, 588…), SZ for Shenzhen (0xx, 3xx, 15x/16x/18x).
    prefix = "SH" if code[0] in ("5", "6", "9") else "SZ"
    em_code = f"{prefix}{code}"

    lines = [f"## {code} 国内散户情绪 (东方财富):\n"]

    # --- 1) 千股千评 (per-stock composite score) ---
    try:
        comment = ak_retry(lambda: ak.stock_comment_em())
        row = comment[comment["代码"].astype(str) == code]
        if not row.empty:
            r = row.iloc[0]
            lines.append("### 千股千评（综合情绪评分）")
            score = _fmt_num(r.get("综合得分"))
            attention = _fmt_num(r.get("关注指数"))
            rank = _fmt_num(r.get("目前排名"))
            rising = _fmt_num(r.get("上升"))
            inst = _fmt_num(r.get("机构参与度"))
            if score is not None:
                lines.append(f"- 综合得分: {score:.1f}")
            if attention is not None:
                lines.append(f"- 关注指数: {attention:.1f}")
            if rank is not None:
                lines.append(f"- 人气排名: {int(rank)}")
            if rising is not None:
                lines.append(f"- 关注度变化: {'上升' if rising > 0 else '下降'} ({int(rising)})")
            if inst is not None:
                lines.append(f"- 机构参与度: {inst:.3f}")
            lines.append("")
        else:
            lines.append("### 千股千评：该代码未进入今日千股千评列表（可能停牌或无评分）\n")
    except VendorUnavailableError as e:
        lines.append(f"### 千股千评：<unavailable: {e}>\n")

    # --- 2) 人气榜 ---
    try:
        hot_rank = ak_retry(lambda: ak.stock_hot_rank_em())
        hit = hot_rank[hot_rank["代码"].astype(str).str[-6:] == code]
        if not hit.empty:
            h = hit.iloc[0]
            lines.append("### 人气榜（全市场热度排名）")
            lines.append(f"- 当前排名: {h.get('当前排名')}（共 {len(hot_rank)} 只）")
            lines.append(f"- 最新价: {h.get('最新价')}，涨跌幅: {h.get('涨跌幅')}%")
            lines.append("")
        else:
            lines.append("### 人气榜：未进入今日人气榜 TOP100\n")
    except VendorUnavailableError as e:
        lines.append(f"### 人气榜：<unavailable: {e}>\n")

    # --- 3) 热搜关键词 ---
    try:
        kw = ak_retry(lambda: ak.stock_hot_keyword_em(symbol=em_code))
        if kw is not None and not kw.empty:
            lines.append("### 热搜关联概念（热度）")
            for _, k in kw.head(6).iterrows():
                concept = str(k.get("概念名称") or "").strip()
                heat = _fmt_num(k.get("热度"))
                if concept and heat is not None:
                    lines.append(f"- {concept}: {heat:.0f}")
            lines.append("")
        else:
            lines.append("### 热搜关键词：无关联热搜概念\n")
    except VendorUnavailableError as e:
        lines.append(f"### 热搜关键词：<unavailable: {e}>\n")

    if len(lines) == 2:  # only the header — everything failed
        raise VendorUnavailableError(f"Eastmoney sentiment endpoints returned no data for {code}")

    return "\n".join(lines).rstrip()


def _get_etf_sentiment(ticker: str) -> str:
    """Sector-aggregate sentiment for an ETF.

    An ETF is a fund wrapper, not a stock, so it has no 千股千评 row and no
    人气榜 position. What has sentiment is the *sector it tracks*: the top
    constituent stocks. We aggregate their 千股千评 composite scores into a
    sector read (平均/最高/最低得分, 平均关注指数), and also pull the ETF's own
    fund-flow (资金流) as the money-side confirmation. This gives the analyst a
    concrete, machine-readable "how is the tracked industry feeling" signal
    instead of an empty "未进入列表".
    """
    import akshare as ak

    code = normalize_ak_symbol(ticker)
    name = get_etf_name(code) or code
    industry = get_etf_industry_keyword(code)

    lines = [f"## {name} ({code}) 行业情绪 (东方财富):"]
    if industry:
        lines.append(f"跟踪行业: {industry}")

    holdings = get_etf_holdings(code)

    # --- 1) Aggregate 千股千评 across top holdings ---
    scores, attentions, names = [], [], []
    comment = None
    try:
        comment = ak_retry(lambda: ak.stock_comment_em())
    except VendorUnavailableError as e:
        lines.append(f"\n### 千股千评（行业聚合）：<unavailable: {e}>")

    if comment is not None and not comment.empty and holdings:
        code_col = "代码"
        comment = comment.copy()
        comment[code_col] = comment[code_col].astype(str)
        for hcode, hname in holdings:
            row = comment[comment[code_col] == hcode]
            if row.empty:
                continue
            r = row.iloc[0]
            sc = _fmt_num(r.get("综合得分"))
            at = _fmt_num(r.get("关注指数"))
            if sc is not None:
                scores.append(sc)
                attentions.append(at if at is not None else 0.0)
                names.append((hname or hcode, sc, at))
        if scores:
            avg = sum(scores) / len(scores)
            hi = max(scores)
            lo = min(scores)
            avg_att = sum(attentions) / len(attentions) if attentions else None
            lines.append("\n### 千股千评（跟踪行业成分股聚合）")
            lines.append(f"- 覆盖成分股: {len(scores)} 只")
            lines.append(f"- 平均综合得分: {avg:.1f}（{_score_band(avg)}）")
            lines.append(f"- 最高得分: {hi:.1f}，最低得分: {lo:.1f}")
            if avg_att is not None:
                lines.append(f"- 平均关注指数: {avg_att:.1f}")
            lines.append("")
            lines.append("成分股明细（得分 / 关注指数）:")
            for nm, sc, at in sorted(names, key=lambda x: -x[1]):
                at_txt = f"{at:.1f}" if at is not None else "—"
                lines.append(f"- {nm}: 得分 {sc:.1f}，关注 {at_txt}")
        else:
            lines.append("\n### 千股千评（行业聚合）：成分股暂无评分")
    elif not holdings:
        lines.append("\n### 千股千评（行业聚合）：持仓数据不可用")

    # --- 2) The ETF's own fund flow (money-side read) ---
    try:
        fund_flow = ak_retry(lambda: ak.stock_individual_fund_flow(stock=code, market="sh" if code[0] == "5" else "sz"))
        if fund_flow is not None and not fund_flow.empty:
            fund_flow = fund_flow.copy()
            fund_flow["日期"] = fund_flow["日期"].astype(str)
            latest = fund_flow.sort_values("日期").iloc[-1]
            col = "主力净流入-净额"
            if col in latest.index or col in fund_flow.columns:
                net = _fmt_num(latest.get(col))
                if net is not None:
                    yi = net / 100_000_000.0
                    sign = "净流入" if net >= 0 else "净流出"
                    lines.append("\n### 基金主力资金流（当日）")
                    lines.append(f"- 主力净流入: {yi:+.2f} 亿元（{sign}）")
            else:
                lines.append("\n### 基金主力资金流：字段缺失")
        else:
            lines.append("\n### 基金主力资金流：暂无数据")
    except VendorUnavailableError as e:
        lines.append(f"\n### 基金主力资金流：<unavailable: {e}>")

    return "\n".join(lines).rstrip()


def _score_band(score: float) -> str:
    """A human label for a 0–100 千股千评 composite score."""
    if score >= 80:
        return "偏多"
    if score >= 60:
        return "中性偏多"
    if score >= 40:
        return "中性"
    return "偏空"
