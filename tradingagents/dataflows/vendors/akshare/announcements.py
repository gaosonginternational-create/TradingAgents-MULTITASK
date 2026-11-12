"""A-share company announcements and earnings guidance.

Two capabilities the fundamentals analyst needs that the plain financial
statements do not provide:

1. **Announcements** — the *unstructured* company disclosures (业绩预告/快报、
   业绩指引、重大合同、产能扩张、股权激励、增减持、回购等) via Eastmoney's
   per-stock announcement feed (``stock_individual_notice_report``) and
   Juchao's disclosure search (``stock_zh_a_disclosure_report_cninfo``).

2. **Earnings guidance** — the *structured* forward-looking figures published in
   业绩预告 (profit forecast: predicted net-profit range, YoY change, reason)
   and 业绩快报 (express report), so the analyst can estimate *future* earnings
   and compare them against the current valuation.

A-share disclosure follows a three-stage rhythm: 业绩预告 (guidance, before the
report) → 业绩快报 (express, quick unaudited numbers) → 正式报告 (audited). The
guidance is the earliest forward signal — it gives a net-profit *range* and a
*reason*, which is exactly what "estimate future earnings from guidance" needs.

``get_earnings_guidance`` returns the company's own guidance. The *downstream
customer / capex* angle (using a customer's financials and capital expenditure
to project a supplier's revenue) is left to the LLM analyst — this module only
delivers the raw disclosure facts; the prompt steers the analyst to connect
them.
"""

from __future__ import annotations

import logging
import re

import pandas as pd

from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.vendors.akshare.common import (
    ak_retry,
    is_a_share,
    normalize_ak_symbol,
    raise_for_empty,
)

logger = logging.getLogger(__name__)

# Announcement categories worth surfacing for fundamental analysis. We fetch the
# broad "全部" once and filter client-side on keywords, because Eastmoney's
# ``symbol`` filter is coarse (7 buckets) and we want to surface the subset that
# moves earnings (guidance, contracts, capacity, buybacks) without dumping the
# full firehose of routine governance notices.
_GUIDANCE_KEYWORDS = (
    "业绩预告", "业绩快报", "业绩预增", "业绩预减", "业绩预亏", "业绩预盈",
    "业绩修正", "业绩指引", "盈利预测", "经营情况", "重大合同", "中标",
    "扩产", "产能", "项目投产", "定增", "增发", "回购", "减持", "增持",
    "股权激励", "分红", "派息", "收购", "并购", "重组", "战略合作",
    "订单", "签订", "供货", "客户",
)

# The report-period suffixes ``stock_yjyg_em`` / ``stock_yjkb_em`` accept.
_REPORT_PERIOD = re.compile(r"^\d{4}(0331|0630|0930|1231)$")


def _to_period(datestr: str) -> str | None:
    """Normalize a yyyy-mm-dd (or yyyymmdd) date to an Eastmoney report period.

    Returns the *latest* report period on or before ``datestr``, so an analyst
    asking "as of 2026-06-01" gets the Q1 (0331) guidance that was current then.
    """
    if not datestr:
        return None
    s = datestr.replace("-", "")
    if not s.isdigit():
        return None
    if len(s) == 8:
        y, m, d = s[:4], s[4:6], s[6:8]
    elif len(s) == 6:  # already a report period like 20250331
        return s if _REPORT_PERIOD.match(s) else None
    else:
        return None
    if _REPORT_PERIOD.match(f"{y}{m}{d}"):
        return f"{y}{m}{d}"
    # Walk back to the nearest quarter-end.
    y, m = int(y), int(m)
    if m <= 3:
        return f"{y}0331" if (y, m) >= (y, 3) else f"{y - 1}1231"
    if m <= 6:
        return f"{y}0630"
    if m <= 9:
        return f"{y}0930"
    return f"{y}1231"


def _match_code(df: pd.DataFrame, code: str) -> pd.DataFrame:
    """Filter a market-wide table down to the target ``code``.

    Eastmoney's per-period tables carry a ``股票代码`` column; values may be
    int or zero-padded str. Match loosely on the 6-digit tail.
    """
    if df is None or df.empty:
        return df
    col = next((c for c in ("股票代码", "代码") if c in df.columns), None)
    if col is None:
        return df
    mask = df[col].astype(str).str.replace(r"\.0$", "", regex=True).str[-6:] == code
    return df[mask]


def get_announcements(
    ticker: str,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 40,
) -> str:
    """Recent company announcements for an A-share ``ticker``.

    Returns a bounded markdown list of announcements (title, category, date,
    URL) drawn from Eastmoney's per-stock feed, with the earnings-relevant
    subset (guidance, contracts, capacity, buybacks, M&A) surfaced first.
    ``start_date``/``end_date`` are yyyy-mm-dd or yyyymmdd.
    """
    if not is_a_share(ticker):
        raise NoMarketDataError(
            ticker, ticker, f"'{ticker}' is not a recognized A-share code"
        )
    code = normalize_ak_symbol(ticker)

    import akshare as ak

    begin = start_date.replace("-", "") if start_date else None
    end = end_date.replace("-", "") if end_date else None

    rows: list[str] = []
    seen = 0

    # --- Primary: Eastmoney per-stock announcements ---
    try:
        df = ak_retry(lambda: ak.stock_individual_notice_report(
            security=code, symbol="全部", begin_date=begin, end_date=end,
        ))
    except Exception as e:  # noqa: BLE001
        logger.warning("Eastmoney notice report failed for %s: %s", code, e)
        df = None

    if df is not None and not df.empty:
        # Sort so earnings-relevant notices float to the top.
        def relevance(title: str) -> int:
            t = str(title or "")
            return 0 if any(k in t for k in _GUIDANCE_KEYWORDS) else 1

        df = df.copy()
        if "公告标题" in df.columns:
            df["_rel"] = df["公告标题"].map(relevance)
            df = df.sort_values("_rel").drop(columns="_rel")

        for _, row in df.head(limit).iterrows():
            title = str(row.get("公告标题") or "").strip()
            if not title:
                continue
            cat = str(row.get("公告类型") or "").strip()
            when = str(row.get("公告日期") or "").strip()
            url = str(row.get("网址") or "").strip()
            lines = [f"### {title}"]
            if cat:
                lines.append(f"- 类型: {cat}")
            if when:
                lines.append(f"- 日期: {when}")
            if url:
                lines.append(f"- 链接: {url}")
            rows.append("\n".join(lines))
            seen += 1

    # --- Secondary: Juchao disclosure search (keyword-able, includes 业绩预告) ---
    if seen == 0:
        try:
            jdf = ak_retry(lambda: ak.stock_zh_a_disclosure_report_cninfo(
                symbol=code, market="沪深京", keyword="",
                start_date=(start_date or "20200101").replace("-", ""),
                end_date=(end_date or "20991231").replace("-", ""),
            ))
        except Exception as e:  # noqa: BLE001
            logger.warning("Juchao disclosure search failed for %s: %s", code, e)
            jdf = None

        if jdf is not None and not jdf.empty:
            for _, row in jdf.head(limit).iterrows():
                title = str(row.get("公告标题") or "").strip()
                if not title:
                    continue
                when = str(row.get("公告时间") or "").strip()
                url = str(row.get("公告链接") or "").strip()
                lines = [f"### {title}"]
                if when:
                    lines.append(f"- 日期: {when}")
                if url:
                    lines.append(f"- 链接: {url}")
                rows.append("\n".join(lines))
                seen += 1

    if seen == 0:
        raise VendorUnavailableError(f"No announcements found for {code}")

    header = [f"# 公司公告 Announcements for {code}（来自 {ticker}）", ""]
    return "\n".join(header) + "\n".join(rows)


def get_earnings_guidance(
    ticker: str,
    as_of_date: str | None = None,
) -> str:
    """Company earnings guidance / express report for an A-share ``ticker``.

    Pulls the structured 业绩预告 (profit forecast: predicted net-profit range,
    YoY change, reason, type) and 业绩快报 (express report) for the latest
    report period on or before ``as_of_date``, filtering the market-wide tables
    down to this company. This is the forward earnings signal the analyst uses
    to estimate *future* profit and compare it to the current valuation.
    """
    if not is_a_share(ticker):
        raise NoMarketDataError(
            ticker, ticker, f"'{ticker}' is not a recognized A-share code"
        )
    code = normalize_ak_symbol(ticker)

    import akshare as ak

    period = _to_period(as_of_date)
    parts: list[str] = []

    # --- 业绩预告 (guidance: predicted profit range + reason) ---
    try:
        yg = ak_retry(lambda: ak.stock_yjyg_em(date=period or ""))
        yg = _match_code(yg, code)
        if yg is not None and not yg.empty:
            parts.append(_render_guidance(code, ticker, yg))
    except Exception as e:  # noqa: BLE001
        logger.warning("业绩预告 unavailable for %s: %s", code, e)

    # --- 业绩快报 (express report: near-final quick numbers) ---
    try:
        kb = ak_retry(lambda: ak.stock_yjkb_em(date=period or ""))
        kb = _match_code(kb, code)
        if kb is not None and not kb.empty:
            parts.append(_render_express(code, ticker, kb))
    except Exception as e:  # noqa: BLE001
        logger.warning("业绩快报 unavailable for %s: %s", code, e)

    if not parts:
        raise_for_empty(ticker, code, f"earnings guidance for period {period or 'latest'}")

    return (
        f"# 业绩指引与快报 Earnings Guidance for {code}（来自 {ticker}）\n"
        f"报告期: {period or '最新'}\n\n" + "\n\n".join(parts)
    )


def _yi(v):
    """Render a yuan value as 亿元, or '—' if missing."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    return f"{float(v) / 1e8:.2f} 亿元"


def _pct(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    return f"{float(v):.2f}%"


def _render_guidance(code: str, ticker: str, df: pd.DataFrame) -> str:
    """Render the 业绩预告 rows: predicted net-profit range, change, reason.

    Eastmoney's 业绩预告 table carries one row per *forecast metric* (每股收益 /
    扣非净利润 / 归母净利润), so the same company can appear several times. We
    surface the net-profit rows (the forward-earnings anchor) and fold the EPS
    figure in where present, labelling the metric explicitly so the analyst does
    not confuse a per-share figure with a total.
    """
    lines = ["## 业绩预告（公司指引）", ""]
    for _, row in df.iterrows():
        name = str(row.get("股票简称") or "").strip()
        metric = str(row.get("预测指标") or "").strip()
        yoy = row.get("业绩变动幅度")
        val = row.get("预测数值")
        prev = row.get("上年同期值")
        reason = str(row.get("业绩变动原因") or "").strip()
        typ = str(row.get("预告类型") or "").strip()
        when = str(row.get("公告日期") or "").strip()

        is_eps = "每股收益" in metric
        is_profit = "净利润" in metric

        head = f"### {name or ticker}"
        if typ:
            head += f"（{typ}）"
        lines.append(head)

        # The value's unit depends on the metric: net profit is in yuan, EPS in
        # yuan/share. Report each with the right label and scale.
        if val is not None and not pd.isna(val):
            if is_eps:
                lines.append(f"- 预测每股收益: {float(val):.2f} 元/股")
            elif is_profit:
                lines.append(f"- 预测净利润: {_yi(val)}")
            else:
                lines.append(f"- 预测值({metric}): {_yi(val)}")
        if yoy is not None and not pd.isna(yoy):
            lines.append(f"- 业绩变动幅度: {_pct(yoy)}")
        if prev is not None and not pd.isna(prev):
            label = "上年同期每股收益" if is_eps else "上年同期净利润"
            lines.append(f"- {label}: {_yi(prev)}" if not is_eps else f"- {label}: {float(prev):.2f} 元/股")
        if reason:
            lines.append(f"- 变动原因: {reason[:400]}")
        if when:
            lines.append(f"- 公告日期: {when}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _render_express(code: str, ticker: str, df: pd.DataFrame) -> str:
    """Render the 业绩快报 rows: near-final revenue / net profit and YoY."""
    lines = ["## 业绩快报（Express Report）", ""]
    for _, row in df.iterrows():
        name = str(row.get("股票简称") or "").strip()
        rev = row.get("营业收入-营业收入")
        rev_yoy = row.get("营业收入-同比增长")
        ni = row.get("净利润-净利润")
        ni_yoy = row.get("净利润-同比增长")
        eps = row.get("每股收益")
        when = str(row.get("公告日期") or "").strip()

        lines.append(f"### {name or ticker}")
        if rev is not None and not pd.isna(rev):
            lines.append(f"- 营业收入: {_yi(rev)}")
        if rev_yoy is not None and not pd.isna(rev_yoy):
            lines.append(f"- 营收同比: {float(rev_yoy):.2f}%")
        if ni is not None and not pd.isna(ni):
            lines.append(f"- 净利润: {_yi(ni)}")
        if ni_yoy is not None and not pd.isna(ni_yoy):
            lines.append(f"- 净利润同比: {float(ni_yoy):.2f}%")
        if eps is not None and not pd.isna(eps):
            lines.append(f"- 每股收益: {eps} 元")
        if when:
            lines.append(f"- 公告日期: {when}")
        lines.append("")
    return "\n".join(lines).rstrip()
