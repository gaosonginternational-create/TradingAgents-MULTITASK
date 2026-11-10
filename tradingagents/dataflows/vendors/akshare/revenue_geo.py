"""Revenue geography for Chinese-market instruments.

The macro weighting the user asked for — "国内营收为主 → 看国内宏观；海外营收
为主 → 看美国宏观" — needs one fact per instrument: *where does its revenue
come from?* Eastmoney's 主营构成 (``stock_zygc_em``) reports, for every A-share,
a "按地区分类" breakdown (国内/国外 or 内销/外销) with 收入比例. We read the
latest reported period and turn it into a ``(domestic_pct, overseas_pct)`` pair.

An ETF has no revenue of its own; its revenue geography is the geography of its
constituents. We aggregate the top holdings' 国内/国外 splits (weighted by their
占净值比例 where available, else equal-weighted) into a single ETF-level read.
That lets the analyst reason "this 科创半导体ETF's constituents sell mostly to
domestic customers, so China industrial policy / PMI / capex matters more than
US chip demand" — instead of naively extrapolating overseas sector trends.
"""

from __future__ import annotations

import logging

from tradingagents.dataflows.vendors.akshare.common import ak_retry, is_a_share, is_etf
from tradingagents.dataflows.vendors.akshare.etf_industry import get_etf_holdings

logger = logging.getLogger(__name__)


def _parse_ratio(v) -> float | None:
    """Parse a ratio cell; Eastmoney gives 收入比例 as 0..1 floats."""
    if v is None:
        return None
    try:
        f = float(v)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


def _region_name(raw: str) -> str:
    """Classify a 主营构成 region label as 'domestic' or 'overseas'."""
    s = str(raw or "").strip()
    if not s:
        return ""
    # "其中:XXX" rows are sub-breakdowns of a parent "其他(补充)" row — exclude
    # them to avoid double-counting.
    if s.startswith("其中") or "补充" in s:
        return ""
    # Domestic markers. Order matters: "中国区"/"中国大陆" before the broad
    # "中国" (which also appears inside "中国港澳台" — still domestic).
    if any(k in s for k in ("国内", "内销", "境内", "中国大陆", "中国区", "华东", "华北", "华南",
                            "华中", "东北", "西南", "西北", "港澳台")):
        return "domestic"
    # Overseas markers.
    if any(k in s for k in ("国外", "外销", "境外", "海外", "出口", "国际", "欧美",
                            "北美", "美国区", "欧洲", "欧亚", "亚太", "东南亚", "亚洲",
                            "非洲", "拉美", "中东", "其他境外")):
        return "overseas"
    # A bare "其他" or unmatched label: ambiguous — skip.
    return ""


def get_revenue_geo(ticker: str) -> dict | None:
    """Return ``{"domestic": 0..1, "overseas": 0..1, "period": "..."}`` for an
    A-share, or ``None`` when unavailable.

    Uses the latest 按地区分类 row group from ``stock_zygc_em``. The domestic
    and overseas fractions are normalised to sum to 1 over the recognised
    regions (unrecognised "其他" rows are excluded from the normalisation so they
    don't dilute the split).
    """
    if not is_a_share(ticker):
        return None
    import akshare as ak

    prefix = "SH" if ticker[0] in ("5", "6", "9") else "SZ"
    symbol = f"{prefix}{ticker}"

    try:
        df = ak_retry(lambda: ak.stock_zygc_em(symbol=symbol))
    except Exception as e:  # noqa: BLE001
        logger.warning("Revenue geo lookup failed for %s: %s", ticker, e)
        return None

    if df is None or df.empty or "分类类型" not in df.columns:
        return None

    region_rows = df[df["分类类型"].astype(str).str.contains("按地区")]
    if region_rows.empty:
        return None

    # Use the latest 报告日期.
    latest_date = region_rows["报告日期"].max()
    latest = region_rows[region_rows["报告日期"] == latest_date]

    domestic = overseas = 0.0
    for _, row in latest.iterrows():
        kind = _region_name(row.get("主营构成"))
        ratio = _parse_ratio(row.get("收入比例"))
        if not kind or ratio is None:
            continue
        if kind == "domestic":
            domestic += ratio
        elif kind == "overseas":
            overseas += ratio

    total = domestic + overseas
    if total <= 0:
        return None
    period = str(latest_date)[:10]
    return {
        "domestic": domestic / total,
        "overseas": overseas / total,
        "period": period,
    }


def get_etf_revenue_geo(ticker: str) -> dict | None:
    """Aggregate revenue geography across an ETF's top holdings.

    Equal-weighted over the holdings that resolve (fund-level 占净值比例 is not
    in the holdings feed we use). Returns the same shape as ``get_revenue_geo``,
    plus a ``holdings`` count, or ``None`` when nothing resolves.
    """
    if not is_etf(ticker):
        return None

    holdings = get_etf_holdings(ticker)
    if not holdings:
        return None

    dom_sum = ov_sum = 0.0
    resolved = 0
    for code, _name in holdings:
        geo = get_revenue_geo(code)
        if not geo:
            continue
        dom_sum += geo["domestic"]
        ov_sum += geo["overseas"]
        resolved += 1

    if resolved == 0:
        return None
    return {
        "domestic": dom_sum / resolved,
        "overseas": ov_sum / resolved,
        "period": "aggregated",
        "holdings": resolved,
    }


def describe_revenue_geo(ticker: str) -> str:
    """A human-readable, prompt-ready description of the revenue geography.

    Returns a concise sentence (or empty string when unknown) that the macro
    weighting guidance can interpolate directly into an analyst prompt.
    """
    geo = get_revenue_geo(ticker) if is_a_share(ticker) else get_etf_revenue_geo(ticker)
    if not geo:
        return ""
    dom = geo["domestic"]
    ov = geo["overseas"]
    if geo.get("holdings"):
        source = f"成分股聚合（{geo['holdings']} 只）"
    else:
        source = f"按地区营收（{geo['period']}）"
    if dom >= 0.75:
        lean = "以国内营收为主"
    elif dom >= 0.55:
        lean = "国内营收略占多数"
    elif ov >= 0.75:
        lean = "以海外营收为主"
    elif ov >= 0.55:
        lean = "海外营收略占多数"
    else:
        lean = "国内/海外营收较为均衡"
    return (
        f"营收地域结构（{source}）: 国内约 {dom:.0%}，海外约 {ov:.0%} —— {lean}。"
    )
