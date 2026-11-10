"""AkShare domestic (A-share) money-flow data from 同花顺 (10jqka).

Complements the Eastmoney "千股千评" retail-sentiment snapshot in
``sentiment.py`` with a *capital-flow* read: who is actually buying and
selling — 主力 (institutional / large orders) vs 散户 (retail). Money flow is
a different signal from opinion sentiment: retail can be bullish in the
comment section while 主力 (big money) is quietly distributing, or vice versa.
That divergence is precisely what the sentiment analyst needs to flag.

We use 同花顺's per-stock fund-flow board (``stock_fund_flow_individual``),
which returns the whole market ranked by net inflow. We pull the board and
filter for the target code. Fields (all in 元): 流入资金 / 流出资金 / 净额 /
成交额, plus 最新价 / 涨跌幅 / 换手率.

Note: this is a *snapshot* (current board), not an archived time series, so —
like the Eastmoney comment score — it is a point-in-time read for live
analysis, not a historical backtest input.
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


def _strip_pct(v):
    """Strip a trailing ``%`` from a percentage string (``-0.18%`` -> ``-0.18``)."""
    if v is None:
        return None
    if isinstance(v, str):
        return v.strip().rstrip("%").strip()
    return v


def _parse_yuan(v):
    """Parse a 同花顺 money value into a float number of 元.

    The board returns strings with a unit suffix, e.g. ``"9.99亿"``,
    ``"-3301.81万"``, ``"20.68亿"``, or a bare number. Normalize to 元.
    """
    if v is None:
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        return None if f != f else f
    s = str(v).strip().replace(",", "")
    if not s or s.lower() in ("nan", "none", "null", "-", "--"):
        return None
    try:
        if s.endswith("亿"):
            return float(s[:-1]) * 100_000_000.0
        if s.endswith("万"):
            return float(s[:-1]) * 10_000.0
        return float(s)
    except ValueError:
        return None


def _fmt_yi(v):
    """Format a money value into 亿元 with 2 decimals; None-safe.

    Accepts either a raw float (元) or a 同花顺 unit-suffixed string (``9.99亿``).
    """
    yuan = _parse_yuan(v)
    if yuan is None:
        return None
    return yuan / 100_000_000.0


def get_ths_moneyflow(ticker: str) -> str:
    """同花顺 (10jqka) per-stock money-flow snapshot for a Chinese-market code.

    Returns a formatted markdown block with:
      - 最新价 / 涨跌幅 / 换手率
      - 流入资金 / 流出资金 / 净额 (亿元)
      - 成交额 (亿元) and 净流入占比 (净额 / 成交额)
      - 主力/散户方向解读线索 (net inflow sign + magnitude)

    Degrades gracefully: any upstream failure becomes a clear unavailable
    signal rather than an exception, so the analyst always sees something.
    """
    import akshare as ak

    if not is_cn_market(ticker):
        raise NoMarketDataError(
            ticker, ticker, f"'{ticker}' is not a recognized A-share/HK/ETF code"
        )
    code = normalize_ak_symbol(ticker)  # 600519 (ETFs keep their 6 digits)

    lines = [f"## {code} 资金流向 (同花顺):\n"]

    # 同花顺 stock_fund_flow_individual 只覆盖 A 股；ETF 不在其榜单中。ETF 的
    # 资金流走东财 stock_individual_fund_flow（按标的存档），主力净流入才是
    # 基金层面的正确信号。
    if is_etf(ticker):
        return _get_etf_moneyflow(code, lines)

    try:
        # 同花顺个股资金流排行（即时），全市场按净额排序。
        # 注意：该接口只覆盖 A 股，港股/ETF 可能不在榜单中。
        flow = ak_retry(lambda: ak.stock_fund_flow_individual(symbol="即时"))
    except VendorUnavailableError as e:
        lines.append(f"### 同花顺资金流：<unavailable: {e}>\n")
        return "\n".join(lines).rstrip()

    if flow is None or flow.empty:
        lines.append("### 同花顺资金流：未返回数据\n")
        return "\n".join(lines).rstrip()

    # 股票代码列可能是 int（600111）或 str（"600111"）；统一为 6 位字符串比对。
    code_col = "股票代码"
    if code_col not in flow.columns:
        # 兼容列名变化
        cand = [c for c in flow.columns if "代码" in str(c)]
        code_col = cand[0] if cand else None

    hit = None
    if code_col:
        codes = flow[code_col].astype(str).str.zfill(6)
        hit = flow[codes == code]
    if hit is None or hit.empty:
        lines.append(f"### 同花顺资金流：{code} 未进入今日资金流榜单\n")
        return "\n".join(lines).rstrip()

    r = hit.iloc[0]

    price = _fmt_num(r.get("最新价"))
    pct = _fmt_num(_strip_pct(r.get("涨跌幅")))
    turnover = _fmt_num(_strip_pct(r.get("换手率")))
    inflow = _fmt_yi(r.get("流入资金(元)") if "流入资金(元)" in flow.columns else r.get("流入资金"))
    outflow = _fmt_yi(r.get("流出资金(元)") if "流出资金(元)" in flow.columns else r.get("流出资金"))
    net = _fmt_yi(r.get("净额(元)") if "净额(元)" in flow.columns else r.get("净额"))
    amount = _fmt_yi(r.get("成交额(元)") if "成交额(元)" in flow.columns else r.get("成交额"))

    name = str(r.get("股票简称") or "").strip() or code

    lines.append(f"### {name}（{code}）资金流")
    if price is not None:
        lines.append(f"- 最新价: {price:.2f}")
    if pct is not None:
        lines.append(f"- 涨跌幅: {pct:+.2f}%")
    if turnover is not None:
        lines.append(f"- 换手率: {turnover:.2f}%")
    if inflow is not None:
        lines.append(f"- 流入资金: {inflow:.2f} 亿元")
    if outflow is not None:
        lines.append(f"- 流出资金: {outflow:.2f} 亿元")
    if net is not None:
        sign = "净流入" if net >= 0 else "净流出"
        lines.append(f"- 资金净额: {net:+.2f} 亿元（{sign}）")
    if amount is not None and amount > 0 and net is not None:
        ratio = net / amount * 100
        lines.append(f"- 净流入占比: {ratio:+.2f}%（占成交额）")

    # 方向解读线索：供情绪分析师直接引用，不替它下结论。
    if net is not None:
        if net > 0:
            lines.append("- 解读线索: 主力资金当日净流入，资金面偏多（关注是否与股价/情绪背离）")
        elif net < 0:
            lines.append("- 解读线索: 主力资金当日净流出，资金面偏空（关注是否为高位派发）")
        else:
            lines.append("- 解读线索: 资金净额接近 0，资金面中性/观望")
    lines.append("")

    return "\n".join(lines).rstrip()


def _get_etf_moneyflow(code: str, lines: list[str]) -> str:
    """Fund-level money flow for an ETF (Eastmoney ``stock_individual_fund_flow``).

    The 同花顺 per-stock fund-flow board doesn't list ETFs, so we read the ETF's
    own archived 主力净流入 directly — 当日 net inflow plus the latest close /
    change, mirroring the sentiment vendor's ETF read.
    """
    import akshare as ak

    try:
        market = "sh" if code[0] == "5" else "sz"
        flow = ak_retry(lambda: ak.stock_individual_fund_flow(stock=code, market=market))
    except VendorUnavailableError as e:
        lines.append(f"### 同花顺资金流：<unavailable: {e}>\n")
        return "\n".join(lines).rstrip()

    if flow is None or flow.empty:
        lines.append("### 同花顺资金流：ETF 暂无资金流数据\n")
        return "\n".join(lines).rstrip()

    flow = flow.copy()
    flow["日期"] = flow["日期"].astype(str)
    flow = flow.sort_values("日期")
    latest = flow.iloc[-1]

    col = "主力净流入-净额"
    net = _fmt_yi(latest.get(col)) if col in flow.columns else None
    close = _fmt_num(latest.get("收盘价") if "收盘价" in flow.columns else latest.get("收盘"))
    pct = _fmt_num(latest.get("涨跌幅") if "涨跌幅" in flow.columns else None)

    lines.append("### ETF 主力资金流（东财按标的存档）")
    if close is not None:
        lines.append(f"- 最新收盘: {close:.3f}")
    if pct is not None:
        lines.append(f"- 涨跌幅: {pct:+.2f}%")
    if net is not None:
        sign = "净流入" if net >= 0 else "净流出"
        lines.append(f"- 当日主力净流入: {net:+.2f} 亿元（{sign}）")
    else:
        lines.append("- 主力净流入字段缺失")
    lines.append("")
    return "\n".join(lines).rstrip()
