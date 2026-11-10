"""AkShare "国家队" (national team) broad-index ETF monitoring.

The "national team" (中央汇金 / 中国证金 / 社保基金) rarely buys individual
stocks outright; it funnels support through broad-index ETFs (沪深300 / 上证50 /
科创50 / 中证500 / 中证1000 / 中证A500). Their buy/sell action shows up two
ways, both publicly observable:

  1. **主力资金流** — the ETF's institutional net inflow (主力/超大单), from
     Eastmoney's per-instrument historical fund-flow feed
     (``stock_individual_fund_flow``), which archives daily and lets us compute
     当日 net inflow plus 10-day / 20-day cumulative net inflow.
  2. **份额变化** — ETF shares outstanding (一级市场申赎), which changes when
     money is created/redeemed through the fund. Shenzhen ETFs are archived
     daily (``fund_scale_daily_szse``); Shanghai's official feed has been
     unreliable, so we fall back to the money-flow read for those.

We track a fixed watchlist of the most liquid broad-index ETFs per index. The
analyst gets, for each ETF: 当日主力净流入, 10日/20日累计主力净流入, and (for
Shenzhen ETFs) 当日/10日/20日份额净增减 — a direct "is the national team buying
or selling this week/month" read.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.vendors.akshare.common import ak_retry

logger = logging.getLogger(__name__)

# Core broad-index ETF watchlist. (code, name, market, index). Shanghai ETFs use
# market='sh', Shenzhen 'sz'. These are the most-liquid proxies per index.
NATIONAL_TEAM_ETFS = [
    ("510300", "沪深300ETF", "sh", "沪深300"),
    ("510050", "上证50ETF", "sh", "上证50"),
    ("588000", "科创50ETF", "sh", "科创50"),
    ("512100", "中证1000ETF", "sh", "中证1000"),
    ("510500", "中证500ETF", "sh", "中证500"),
    ("159915", "创业板ETF", "sz", "创业板指"),
    ("159845", "中证1000ETF(深)", "sz", "中证1000"),
]

# Number of trading days to accumulate for the 10-day / 20-day reads.
WINDOWS = (10, 20)


def _fmt_num(v):
    if v is None:
        return None
    try:
        f = float(v)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return v


def _fmt_yi(v):
    """Format a yuan amount into 亿元; None-safe."""
    f = _fmt_num(v)
    if f is None:
        return None
    return f / 100_000_000.0


def _n_days_back(date_str, n):
    """Return a date string ``n`` calendar days before ``date_str``."""
    d = datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=n)
    return d.strftime("%Y-%m-%d")


def _flow_window(stock: str, market: str, start_date: str, end_date: str):
    """Eastmoney per-instrument fund-flow over a window (元 amounts)."""
    import akshare as ak

    df = ak_retry(lambda: ak.stock_individual_fund_flow(stock=stock, market=market))
    if df is None or df.empty:
        return None
    df = df.copy()
    df["日期"] = df["日期"].astype(str)
    mask = (df["日期"] >= start_date) & (df["日期"] <= end_date)
    win = df[mask]
    if win.empty:
        return None
    main = win["主力净流入-净额"].astype(float).sum() if "主力净流入-净额" in win.columns else None
    return main


def get_national_team_flow(end_date: str) -> str:
    """National-team broad-index ETF buy/sell monitor as of ``end_date``.

    For each ETF in the watchlist, reports 当日主力净流入, 10日/20日累计主力
    净流入 (亿元), and (for Shenzhen ETFs) 当日/10日/20日份额净增减. Returns a
    markdown block. Degrades gracefully: a failed ETF is skipped with a note,
    and a total failure yields a clear unavailable signal.
    """
    import akshare as ak

    lines = [f"## 国家队宽基 ETF 动向 (截至 {end_date}):\n"]

    # --- Shenzhen ETF shares (for 份额净增减) ---
    szse_shares = {}
    try:
        start = _n_days_back(end_date, 45)  # enough calendar days to cover 20 trading days
        # fund_scale_daily_szse wants YYYYMMDD (no dashes).
        start_ymd = start.replace("-", "")
        end_ymd = end_date.replace("-", "")
        shares = ak_retry(lambda: ak.fund_scale_daily_szse(start_date=start_ymd, end_date=end_ymd))
        if shares is not None and not shares.empty:
            shares = shares.copy()
            shares["基金代码"] = shares["基金代码"].astype(str)
            shares["日期"] = shares["日期"].astype(str)
            szse_shares = shares.set_index(["基金代码", "日期"])["基金份额"].to_dict()
    except VendorUnavailableError as e:
        logger.warning("Shenzhen ETF shares unavailable: %s", e)

    any_ok = False
    for code, name, market, index in NATIONAL_TEAM_ETFS:
        row = [f"### {name} ({code}) — {index}"]

        # 主力资金流：当日 / 10日 / 20日
        try:
            # 用 45 日历日覆盖约 20 个交易日
            start45 = _n_days_back(end_date, 45)
            df = ak_retry(lambda: ak.stock_individual_fund_flow(stock=code, market=market))
            if df is not None and not df.empty:
                df = df.copy()
                df["日期"] = df["日期"].astype(str)
                # 只保留窗口内
                df = df[(df["日期"] >= start45) & (df["日期"] <= end_date)]
                if not df.empty:
                    df = df.sort_values("日期")
                    col = "主力净流入-净额"
                    if col in df.columns:
                        vals = df[col].astype(float)
                        today = vals.iloc[-1]
                        d10 = vals.tail(10).sum()
                        d20 = vals.tail(20).sum()
                        def _sign(v):
                            return "净流入" if v >= 0 else "净流出"
                        row.append(f"- 当日主力净流入: {_fmt_yi(today):+.2f} 亿元（{_sign(today)}）")
                        row.append(f"- 10日累计主力净流入: {_fmt_yi(d10):+.2f} 亿元（{_sign(d10)}）")
                        row.append(f"- 20日累计主力净流入: {_fmt_yi(d20):+.2f} 亿元（{_sign(d20)}）")
                        any_ok = True
        except VendorUnavailableError as e:
            row.append(f"- 主力资金流：<unavailable: {e}>")

        # 份额变化：仅深交所（sz）可回溯
        # szse_shares 的 key 是 (基金代码, 日期) 元组，需判断是否存在该代码
        has_shares = any(k[0] == code for k in szse_shares)
        if market == "sz" and has_shares:
            try:
                # 提取该代码的份额时间序列
                dates = sorted(k[1] for k in szse_shares if k[0] == code)
                if len(dates) >= 2:
                    def _share(d):
                        return float(szse_shares.get((code, d), 0.0))
                    latest = dates[-1]
                    today_sh = _share(latest)
                    # 10/20 交易日近似：取最近 10/20 个交易日的份额差
                    d10_dates = dates[-10:] if len(dates) >= 10 else dates
                    d20_dates = dates[-20:] if len(dates) >= 20 else dates
                    delta_today = today_sh - _share(dates[-2])
                    delta_10 = today_sh - _share(d10_dates[0])
                    delta_20 = today_sh - _share(d20_dates[0])
                    def _share_sign(v):
                        return "净申购" if v >= 0 else "净赎回"
                    row.append(f"- 当日份额净增减: {delta_today:+,.0f} 份（{_share_sign(delta_today)}）")
                    row.append(f"- 10日份额净增减: {delta_10:+,.0f} 份（{_share_sign(delta_10)}）")
                    row.append(f"- 20日份额净增减: {delta_20:+,.0f} 份（{_share_sign(delta_20)}）")
                    any_ok = True
            except Exception as e:
                row.append(f"- 份额变化：<unavailable: {e}>")

        row.append("")
        lines.extend(row)

    if not any_ok:
        lines.append("### 国家队 ETF 监控：所有数据源均不可用\n")

    return "\n".join(lines).rstrip()
