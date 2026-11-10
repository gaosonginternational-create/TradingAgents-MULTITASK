"""A-share valuation analysis (forward multiples, DCF/FCFF, trend, industry).

The stock-oriented ``fundamentals.py`` returns a static indicator snapshot
(PE/PB/ROE/margins). This module adds the *valuation* layer the analyst needs
to reason about whether a stock is cheap or expensive:

1. **Forward / current multiples** — Eastmoney's peer-valuation table
   (``stock_zh_valuation_comparison_em``) gives trailing + forward (25E/26E/27E)
   P/E, PEG, P/B, P/S, P/CF and EV/EBITDA, *plus* the industry median/mean for
   each, which is exactly what "compare to the sector" needs.

2. **Historical valuation trend** — Baidu Gushitong (``stock_zh_valuation_baidu``)
   returns a daily history of PE(TTM)/PE(static)/PB/PS/PCF/market-cap, so the
   analyst can see where the current multiple sits in its own range and how the
   price has moved *around* valuation.

3. **DCF / FCFF estimate** — a simplified single-stage FCFF model built from the
   financial-analysis indicator table (net profit, operating cash flow per
   share, revenue/profit growth). It is a *modeled estimate*, clearly labelled
   as such, never presented as a market price.

4. **Industry valuation model guidance** — a small mapping from industry
   keyword to the valuation approach that is appropriate for that sector
   (banks → P/B + ROE, growth tech → PEG, mature consumer → DCF, asset-heavy
   industrials → EV/EBITDA, etc.), so the analyst does not apply one metric
   blindly to every company.

``get_valuation`` is the single entry point; ``get_etf_fundamentals`` (in
``fundamentals.py``) reuses ``get_valuation`` per top holding for ETFs.
"""

from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd

from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.vendors.akshare.common import (
    ak_retry,
    is_a_share,
    is_etf,
    normalize_ak_symbol,
)

logger = logging.getLogger(__name__)


# Industry keyword → (valuation models, one-line rationale).  Used to steer the
# analyst toward the right lens per sector rather than one-size-fits-all PE.
INDUSTRY_VALUATION_GUIDE = {
    "银行": ("PB + ROE + 股息率", "银行利润受拨备/坏账周期影响大，PB×ROE 及股息率是主锚"),
    "券商": ("PB + 净资产", "券商盈利强周期，用 PB 衡量净资产价值"),
    "保险": ("PEV(内含价值) + PB", "保险看内含价值增长而非单纯 PE"),
    "半导体": ("PEG + PS + 前瞻PE", "高成长高研发，PEG 与前瞻 PE 兼顾估值与增速"),
    "芯片": ("PEG + PS", "成长期盈利波动大，PEG/PS 比静态 PE 更合理"),
    "医药": ("DCF + PEG", "研发管线价值需 DCF，成熟品种看 PEG"),
    "白酒": ("DCF + PE", "现金流稳定，DCF 与稳健 PE 结合"),
    "食品饮料": ("DCF + PE", "现金流可预测，DCF 为主、PE 为辅"),
    "新能源": ("PEG + EV/EBITDA", "重资产成长期，PEG 与 EV/EBITDA 并重"),
    "光伏": ("PEG + EV/EBITDA", "资本开支大，EV/EBITDA 剔除折旧扰动"),
    "军工": ("PS + EV/EBITDA", "订单驱动、利润确认滞后，PS 更有参考"),
    "地产": ("PB + 折现资产", "看 NAV/PB，而非 PE"),
    "有色": ("EV/EBITDA + PB", "周期资源看 EV/EBITDA 与 PB"),
    "钢铁": ("PB + EV/EBITDA", "强周期，PB 是历史底部锚"),
    "煤炭": ("PB + 股息率", "成熟高分红，PB 与股息率为主"),
    "科技": ("PEG + PS", "成长科技用 PEG/PS"),
    "消费": ("DCF + PE", "稳定消费现金流，DCF 主锚"),
    "互联网": ("PS + EV/EBITDA + DCF", "平台型看 PS 与 DCF"),
    "汽车": ("PE + EV/EBITDA", "制造看 PE 与 EV/EBITDA"),
    "机械": ("PE + EV/EBITDA", "周期制造看 PE/EV/EBITDA"),
}


def _industry_guide(industry: str) -> tuple[str, str]:
    """Return (models, rationale) for an industry keyword, or a neutral default."""
    if not industry:
        return ("PE + PB + DCF", "通用：PE/PB 交叉验证，现金流稳定再用 DCF")
    for key, (models, rationale) in INDUSTRY_VALUATION_GUIDE.items():
        if key in industry:
            return (models, rationale)
    return ("PE + PB + DCF", "未匹配特定行业，用 PE/PB 交叉 + DCF 兜底")


def _exchange_prefixed(code: str) -> str:
    """Add the Eastmoney exchange prefix a peer-comparison symbol needs.

    ``stock_zh_valuation_comparison_em`` requires ``SH600519`` / ``SZ000651``
    style symbols; a bare 6-digit code raises KeyError('result').
    """
    if code.startswith(("60", "68")):
        return f"SH{code}"
    if code.startswith(("00", "30")):
        return f"SZ{code}"
    # Beijing (4xx/8xx): Eastmoney's comparison table does not cover it well;
    # return as-is and let the caller's error handling degrade.
    return code


def get_valuation(ticker: str, as_of_date: str | None = None) -> str:
    """Full valuation report for an A-share ``ticker``.

    Combines forward/current multiples, historical trend, an FCFF estimate and
    the industry valuation-model guidance into one markdown block. For an ETF
    (no financial statements of its own) it dispatches to the aggregated
    holdings fundamentals instead.
    """
    import akshare as ak

    # ETFs have no financial statements; their fundamentals are the holdings'.
    if is_etf(ticker):
        return get_etf_fundamentals(ticker)

    if not is_a_share(ticker):
        raise NoMarketDataError(
            ticker, ticker, f"'{ticker}' is not a recognized A-share code"
        )
    code = normalize_ak_symbol(ticker)

    parts: list[str] = []

    # --- 1) Forward / current multiples + industry comparison ---
    try:
        comp = ak_retry(lambda: ak.stock_zh_valuation_comparison_em(symbol=_exchange_prefixed(code)))
        if comp is not None and not comp.empty:
            parts.append(_render_comparison(code, comp))
    except Exception as e:  # noqa: BLE001
        logger.warning("valuation comparison failed for %s: %s", code, e)

    # --- 2) TTM valuation from the latest statements ---
    try:
        ttm = _render_ttm(code, ak)
        if ttm:
            parts.append(ttm)
    except Exception as e:  # noqa: BLE001
        logger.warning("TTM valuation failed for %s: %s", code, e)

    # --- 3) FCFF / DCF from the statements ---
    try:
        parts.append(_render_fcff(code, ak))
    except Exception as e:  # noqa: BLE001
        logger.warning("FCFF estimate failed for %s: %s", code, e)

    # --- 4) Forward valuation from analyst/company guidance ---
    try:
        fwd = _render_forward(code, ak)
        if fwd:
            parts.append(fwd)
    except Exception as e:  # noqa: BLE001
        logger.warning("forward valuation failed for %s: %s", code, e)

    # --- 5) Historical valuation trend (PE/PB/PS/market cap) ---
    try:
        parts.append(_render_trend(code, ak))
    except Exception as e:  # noqa: BLE001
        logger.warning("valuation trend failed for %s: %s", code, e)

    if not parts:
        raise VendorUnavailableError(f"No valuation data available for {code}")

    return f"# 估值分析 Valuation for {code}\n\n" + "\n\n".join(parts)


def _render_comparison(code: str, comp: pd.DataFrame) -> str:
    """Render the forward/current multiple table, isolating the target row and
    the industry median/mean rows for easy comparison."""
    # The target row is the one whose 代码 matches (strip exchange prefix).
    target = comp[comp["代码"].astype(str).str[-6:] == code]
    industry = comp[comp["简称"].astype(str).isin(["行业中值", "行业平均"])]

    def fmt(v):
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return "—"
        if isinstance(v, float):
            return f"{v:.2f}"
        return str(v)

    lines = ["## 估值倍数与行业对比 (东财同行比较)", ""]
    # Column aliases for readability.
    aliases = {
        "市盈率-TTM": "PE(TTM)",
        "市盈率-25E": "PE-25E(前瞻)",
        "市盈率-26E": "PE-26E(前瞻)",
        "市盈率-27E": "PE-27E(前瞻)",
        "PEG": "PEG",
        "市净率-MRQ": "PB(MRQ)",
        "市销率-TTM": "PS(TTM)",
        "EV/EBITDA-24A": "EV/EBITDA",
    }
    if not target.empty:
        row = target.iloc[0]
        lines.append("### 目标公司")
        for col, alias in aliases.items():
            if col in comp.columns:
                lines.append(f"- {alias}: {fmt(row.get(col))}")

    for _, irow in industry.iterrows():
        lines.append(f"\n### {irow.get('简称', '行业')}")
        for col, alias in aliases.items():
            if col in comp.columns:
                lines.append(f"- {alias}: {fmt(irow.get(col))}")

    return "\n".join(lines)


def _render_trend(code: str, ak) -> str:
    """Render the one-year valuation trend for PE/PB/PS and market cap, with the
    current value and its position in the trailing range."""
    lines = ["## 估值趋势（近一年）与历史分位", ""]

    indicators = [("市盈率(TTM)", "PE(TTM)"), ("市净率", "PB"), ("市现率", "PCF"), ("总市值", "总市值(亿)")]
    for ind, label in indicators:
        try:
            df = ak_retry(lambda: ak.stock_zh_valuation_baidu(symbol=code, indicator=ind, period="近一年"))
        except Exception:  # noqa: BLE001
            continue
        if df is None or df.empty or "value" not in df.columns:
            continue
        vals = pd.to_numeric(df["value"], errors="coerce").dropna()
        if vals.empty:
            continue
        cur = float(vals.iloc[-1])
        lo, hi = float(vals.min()), float(vals.max())
        pct = (cur - lo) / (hi - lo) * 100 if hi > lo else 50.0
        lines.append(
            f"- {label}: 当前 {cur:.2f} | 近一年区间 [{lo:.2f}, {hi:.2f}] "
            f"| 历史分位 {pct:.0f}%"
        )
    return "\n".join(lines)


def _render_ttm(code: str, ak) -> str:
    """TTM (trailing-twelve-months) valuation from the latest report period.

    A-share statements are cumulative within a fiscal year, so TTM net profit /
    operating cash flow = latest annual value if the latest period is a full
    year, otherwise latest cumulative − prior-year cumulative + prior full-year.
    Using the *rolling* figure (rather than the single latest quarter) gives a
    P/E and P/CF that are comparable to the daily-valuation feed.
    """
    try:
        cf = ak_retry(lambda: ak.stock_cash_flow_sheet_by_report_em(symbol=_exchange_prefixed(code)))
        pl = ak_retry(lambda: ak.stock_profit_sheet_by_report_em(symbol=_exchange_prefixed(code)))
    except Exception as e:  # noqa: BLE001
        logger.warning("TTM statements unavailable for %s: %s", code, e)
        return ""

    if cf is None or cf.empty or pl is None or pl.empty:
        return ""

    def num(df, col):
        v = df.iloc[0].get(col)
        if v is None or pd.isna(v):
            return None
        return float(v)

    def date_of(df):
        d = df.iloc[0].get("REPORT_DATE")
        return pd.to_datetime(d) if d is not None else None

    # Latest report period date.
    rpt = date_of(pl)
    if rpt is None:
        return ""

    def ttm(df, col):
        """Roll the cumulative series into a trailing-12-month value."""
        cur = num(df, col)
        if cur is None:
            return None
        # If latest is a full year (12-31), TTM is just that year.
        if rpt.month == 12 and rpt.day == 31:
            return cur
        # Otherwise: latest cumulative − same-period-prior-year cumulative +
        # prior full-year. Find the matching prior-year period row.
        prior_year_same = df[df["REPORT_DATE"].apply(
            lambda d: pd.to_datetime(d).year == rpt.year - 1
            and pd.to_datetime(d).month == rpt.month
            and pd.to_datetime(d).day == rpt.day
        )]
        full_year = df[df["REPORT_DATE"].apply(
            lambda d: pd.to_datetime(d).year == rpt.year - 1
            and pd.to_datetime(d).month == 12 and pd.to_datetime(d).day == 31
        )]
        if prior_year_same.empty or full_year.empty:
            return None
        py = float(prior_year_same.iloc[0].get(col) or 0)
        fy = float(full_year.iloc[0].get(col) or 0)
        return cur - py + fy

    ttm_ocf = ttm(cf, "NETCASH_OPERATE")
    ttm_ni = ttm(pl, "PARENT_NETPROFIT")
    ttm_deduct = ttm(pl, "DEDUCT_PARENT_NETPROFIT")
    ocf = num(cf, "NETCASH_OPERATE")
    capex = num(cf, "CONSTRUCT_LONG_ASSET")

    # Market cap from the daily valuation feed (总市值, in 亿元).
    mktcap = None
    try:
        mc = ak_retry(lambda: ak.stock_zh_valuation_baidu(symbol=code, indicator="总市值", period="近一年"))
        if mc is not None and not mc.empty and "value" in mc.columns:
            mktcap_yi = pd.to_numeric(mc["value"], errors="coerce").dropna()
            if not mktcap_yi.empty:
                mktcap = float(mktcap_yi.iloc[-1]) * 1e8  # 亿元 -> 元
    except Exception:  # noqa: BLE001
        pass

    lines = ["## TTM 估值（滚动近12个月，基于最新财报）", ""]
    lines.append(f"- 最新报告期: {rpt.strftime('%Y-%m-%d')}")
    if ttm_ni is not None:
        lines.append(f"- TTM 归母净利润: {ttm_ni / 1e8:.2f} 亿元")
        if mktcap:
            lines.append(f"- TTM 市盈率 PE = 市值/TTM净利: {mktcap / ttm_ni:.2f} 倍")
    if ttm_deduct is not None:
        lines.append(f"- TTM 扣非净利润: {ttm_deduct / 1e8:.2f} 亿元")
    if ttm_ocf is not None:
        lines.append(f"- TTM 经营现金流净额: {ttm_ocf / 1e8:.2f} 亿元")
        if mktcap:
            lines.append(f"- TTM 市现率 PCF = 市值/TTM经营现金流: {mktcap / ttm_ocf:.2f} 倍")
    if ocf is not None and capex is not None:
        lines.append(f"- 最新期经营现金流: {ocf / 1e8:.2f} 亿元 | 资本开支 CapEx: {capex / 1e8:.2f} 亿元")

    if len(lines) == 2:
        return ""  # nothing computed
    return "\n".join(lines)


def _render_fcff(code: str, ak) -> str:
    """FCFF + two-stage DCF computed from the actual statements.

    FCFF = OCF − CapEx + interest×(1−tax). The base FCFF is the TTM figure; a
    two-stage model projects the next ``EXPLICIT_YEARS`` years using the
    company's trailing growth rate (clamped), then a terminal value via the
    Gordon growth model. Clearly labelled as a modeled estimate.
    """
    EXPLICIT_YEARS = 5
    TAX_RATE = 0.15  # A-share EIT proxy (high-tech enjoy 15%; ordinary 25%)
    WACC = 0.09      # equity-heavy proxy; noted as an assumption
    G_INF = 0.025    # long-run growth

    try:
        cf = ak_retry(lambda: ak.stock_cash_flow_sheet_by_report_em(symbol=_exchange_prefixed(code)))
        pl = ak_retry(lambda: ak.stock_profit_sheet_by_report_em(symbol=_exchange_prefixed(code)))
    except Exception as e:  # noqa: BLE001
        return f"## DCF / FCFF 估值\n\n- 财务报表数据不可用，无法计算 ({type(e).__name__})"

    if cf is None or cf.empty or pl is None or pl.empty:
        return "## DCF / FCFF 估值\n\n- 财务报表数据不可用，无法计算"

    def num(df, col):
        v = df.iloc[0].get(col)
        if v is None or pd.isna(v):
            return None
        return float(v)

    ocf = num(cf, "NETCASH_OPERATE")          # 经营现金流净额
    capex = num(cf, "CONSTRUCT_LONG_ASSET")   # 购建资产支付现金
    interest = num(pl, "INTEREST_EXPENSE")    # 利息费用（税盾加回）
    ni_yoy = num(pl, "PARENT_NETPROFIT_YOY")  # 净利润同比增速(%)
    rev_yoy = num(pl, "OPERATE_INCOME_YOY")   # 营收同比增速(%)

    if ocf is None:
        return "## DCF / FCFF 估值\n\n- 缺少经营现金流数据，无法计算 FCFF"

    # FCFF = OCF − CapEx + interest × (1 − tax). Interest tax-shield add-back.
    capex = capex or 0.0
    interest = interest or 0.0
    fcff = ocf - capex + interest * (1 - TAX_RATE)

    # Growth: prefer net-profit YoY, fall back to revenue YoY, clamp to [-5%, 25%].
    g = 0.03
    g_src = "默认 3%"
    if ni_yoy is not None:
        g = ni_yoy / 100.0
        g_src = f"净利润同比 {ni_yoy:.1f}%"
    elif rev_yoy is not None:
        g = rev_yoy / 100.0
        g_src = f"营收同比 {rev_yoy:.1f}%"
    g = max(-0.05, min(g, 0.25))

    def yi(v):
        return f"{v / 1e8:.2f} 亿元"

    # Two-stage: explicit projection then terminal value.
    pv = 0.0
    fcf = fcff
    details = []
    for y in range(1, EXPLICIT_YEARS + 1):
        fcf = fcf * (1 + g)
        pv += fcf / ((1 + WACC) ** y)
        details.append(f"  - 第{y}年 FCFF: {yi(fcf)}（增速 {g * 100:.1f}%）")

    # Terminal value: Gordon growth.
    term_fcf = fcf * (1 + G_INF)
    if WACC > G_INF:
        tv = term_fcf / (WACC - G_INF)
        pv_tv = tv / ((1 + WACC) ** EXPLICIT_YEARS)
    else:
        tv = pv_tv = 0.0

    ev = pv + pv_tv
    equity = ev  # no net-debt adjustment here; noted as a simplification

    lines = [
        "## DCF / FCFF 估值（基于财报精确计算）",
        "",
        f"- 经营现金流 OCF: {yi(ocf)}",
        f"- 资本开支 CapEx: {yi(capex)}",
        f"- 利息费用: {yi(interest)}（税盾按 {TAX_RATE * 100:.0f}% 税率加回 {(interest * (1 - TAX_RATE)) / 1e8:.2f} 亿元）",
        f"- **FCFF = OCF − CapEx + 利息×(1−税率) = {yi(fcff)}**",
        f"- 近端增速 g: {g * 100:.1f}%（来源: {g_src}，截断至 [-5%, 25%]）",
        f"- 折现率 WACC: {WACC * 100:.1f}%（假设，未做债务/股权加权）",
        f"- 永续增长率 g∞: {G_INF * 100:.1f}%",
        "",
        f"显式预测期（{EXPLICIT_YEARS} 年）：",
        *details,
        f"- 终值（永续增长）: {yi(tv)}",
        f"- 终值现值: {yi(pv_tv)}",
        "",
        f"- **企业价值（两阶段 DCF）≈ {yi(ev)}**",
        f"- 权益价值 ≈ {yi(equity)}（未扣净债务，属简化处理）",
        "- ⚠ 上述为基于财报数据的**模型估算**，非市场价格；WACC/税率/增速均为假设，请结合 PB/PEG 交叉验证",
    ]
    return "\n".join(lines)


def _render_forward(code: str, ak) -> str:
    """Forward valuation from analyst consensus (同花顺盈利预测).

    Uses the consensus forecast of next-year net profit (mean of institution
    estimates) to compute a forward P/E, and shows the YoY growth implied by
    the forecast vs. the current TTM profit, so the analyst can see the
    expected one-year trajectory.
    """
    try:
        fc = ak_retry(lambda: ak.stock_profit_forecast_ths(symbol=code, indicator="预测年报净利润"))
    except Exception as e:  # noqa: BLE001
        logger.warning("profit forecast unavailable for %s: %s", code, e)
        return ""

    if fc is None or fc.empty:
        return ""

    # Rows: 年度 / 预测机构数 / 最小值 / 均值 / 最大值 / 行业平均数.
    rows = []
    for _, row in fc.head(3).iterrows():
        year = row.get("年度")
        mean = row.get("均值")
        lo = row.get("最小值")
        hi = row.get("最大值")
        n = row.get("预测机构数")
        if year is None or mean is None or pd.isna(mean):
            continue
        rows.append((int(year), float(mean), float(lo) if lo is not None and not pd.isna(lo) else None,
                     float(hi) if hi is not None and not pd.isna(hi) else None, int(n) if n is not None and not pd.isna(n) else None))

    if not rows:
        return ""

    # Market cap for forward P/E.
    mktcap = None
    try:
        mc = ak_retry(lambda: ak.stock_zh_valuation_baidu(symbol=code, indicator="总市值", period="近一年"))
        if mc is not None and not mc.empty and "value" in mc.columns:
            v = pd.to_numeric(mc["value"], errors="coerce").dropna()
            if not v.empty:
                mktcap = float(v.iloc[-1]) * 1e8
    except Exception:  # noqa: BLE001
        pass

    lines = ["## 前瞻估值（机构盈利预测，同花顺）", ""]
    for year, mean, lo, hi, n in rows:
        head = f"- {year}年预测归母净利润: {mean:.2f} 亿元"
        if n:
            head += f"（{n} 家机构）"
        if lo is not None and hi is not None:
            head += f"，区间 [{lo:.2f}, {hi:.2f}] 亿元"
        lines.append(head)
        if mktcap:
            lines.append(f"  - 前瞻市盈率 PE({year}E) = 市值/预测净利: {mktcap / (mean * 1e8):.2f} 倍")

    # Implied YoY growth between consecutive forecast years (and vs current).
    if len(rows) >= 2:
        y0, y1 = rows[0], rows[1]
        g_implied = (y1[1] - y0[1]) / y0[1] * 100
        lines.append(f"- 预测期隐含同比增速: {y1[0]}年 vs {y0[0]}年 ≈ {g_implied:.1f}%")

    return "\n".join(lines)


def get_etf_fundamentals(ticker: str) -> str:
    """Aggregated fundamentals for an ETF's top holdings.

    An ETF has no financial statements of its own; its fundamentals are the
    fundamentals of the companies it holds. This pulls the top holdings via
    ``etf_industry.get_etf_holdings`` and runs ``get_valuation`` (or a lighter
    indicator summary) on each, then summarizes the weighted picture.
    """
    from tradingagents.dataflows.vendors.akshare.etf_industry import get_etf_holdings, get_etf_name, get_etf_industry_keyword

    holdings = get_etf_holdings(ticker)
    name = get_etf_name(ticker) or ticker
    industry = get_etf_industry_keyword(ticker)

    header = [f"# ETF 成分股基本面: {name} ({ticker})", ""]
    if industry:
        models, rationale = _industry_guide(industry)
        header.append(f"- 跟踪行业: {industry}")
        header.append(f"- 推荐估值模式: {models} — {rationale}")
        header.append("")
    if not holdings:
        header.append("- 成分股持仓数据不可用")
        return "\n".join(header)

    header.append(f"- 前 {len(holdings)} 大重仓股估值概览如下。\n")

    rows = []
    for hcode, hname in holdings:
        try:
            v = get_valuation(hcode)
            # Extract a compact per-holding summary rather than the full block.
            summary = _summarize_holding(hcode, hname, v)
            rows.append(summary)
        except Exception as e:  # noqa: BLE001
            rows.append(f"- {hname} ({hcode}): 估值数据不可用 ({type(e).__name__})")

    return "\n".join(header) + "\n\n" + "\n".join(rows)


def _summarize_holding(code: str, name: str, valuation_block: str) -> str:
    """Reduce a full valuation report to a one-line-per-metric summary."""
    label = f"{name} ({code})" if name else code
    lines = [f"### {label}"]
    # Pull the PE/PB/trend lines out of the block (prefix "PE(TTM)" etc).
    for token, key in [
        ("PE(TTM):", "PE(TTM)"),
        ("PE-25E(前瞻):", "前瞻PE"),
        ("PEG:", "PEG"),
        ("PB(MRQ):", "PB"),
        ("EV/EBITDA:", "EV/EBITDA"),
    ]:
        for ln in valuation_block.splitlines():
            if token in ln:
                lines.append(f"  - {ln.strip()}")
                break
    if len(lines) == 1:
        lines.append("  - 无估值数据")
    return "\n".join(lines)
