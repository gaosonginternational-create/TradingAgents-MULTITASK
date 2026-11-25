"""Capital flow & national-team positioning for A-shares.

The user's Tick Stock Panel holds quotes, bars, indicators and a factor library,
but **no money-flow or holder data** (checked: 68 endpoints, no flow/北向/龙虎榜
route, and the frontend bundle has no such strings). So this module fills the gap
from sources that don't go through Eastmoney's push2 — which our IP gets
rate-limited on after a burst:

  · 当日资金流：同花顺全市场资金流（一次取回 5000+ 只，含流入/流出/净额/成交额）
  · 国家队：十大股东里的中央汇金 / 中国证券金融 / 社保基金 / 养老金 / 中金，
    按期对比持股比例（季度披露，滞后但方向明确）
  · 龙虎榜：近期是否上榜、机构席位净买卖

Multi-period flow rankings (5日/10日排行) are deliberately **not** used: each one
pages through ~105 requests and takes 20s+, which would dominate an analysis run.

Everything here is best-effort: a source that fails degrades to a line of text,
never an exception — the analyst must be able to say "this input is missing".
"""

from __future__ import annotations

import logging
import os
import time

from tradingagents.dataflows.errors import NoMarketDataError

logger = logging.getLogger(__name__)

_flow_cache: dict[str, object] = {}
_FLOW_TTL = 60.0

# 国家队口径：这些主体被视为"国家队/长期资金"。只做名称包含匹配，避免漏掉
# 不同批次的写法（如"中国证券金融股份有限公司"与"证金公司"）。
_NATIONAL_TEAM_KEYS = (
    "中央汇金", "汇金公司", "中国证券金融", "证金公司",
    "全国社保基金", "社保基金", "基本养老保险", "养老金",
    "中国投资有限责任公司", "中金公司", "梧桐树",
)


def _code(ticker: str) -> str:
    raw = "".join(ch for ch in (ticker or "") if ch.isdigit())
    if len(raw) != 6:
        raise NoMarketDataError(ticker, ticker, f"'{ticker}' is not a 6-digit A-share code")
    return raw


def _market(code: str) -> str:
    return "sh" if code[0] in ("5", "6") else ("bj" if code[0] in ("4", "8") else "sz")


def ths_flow_snapshot() -> dict[str, dict]:
    """Whole-market 当日资金流 from 同花顺, cached for a minute."""
    now = time.time()
    hit = _flow_cache.get("data")
    if hit is not None and now - float(_flow_cache.get("ts") or 0) < _FLOW_TTL:
        return hit  # type: ignore[return-value]
    import akshare as ak

    df = ak.stock_fund_flow_individual(symbol="即时")
    out: dict[str, dict] = {}
    for _, r in df.iterrows():
        code = str(r.get("股票代码", "")).zfill(6)
        out[code] = {
            "name": r.get("股票简称"),
            "price": r.get("最新价"),
            "change_pct": r.get("涨跌幅"),
            "turnover": r.get("换手率"),
            "inflow": r.get("流入资金"),
            "outflow": r.get("流出资金"),
            "net": r.get("净额"),
            "amount": r.get("成交额"),
        }
    _flow_cache["data"] = out
    _flow_cache["ts"] = now
    return out


def _to_yi(value) -> float | None:
    """同花顺返回的是 '3.19亿' / '-6302.16万' 这样的字符串，统一换算成亿元。"""
    if value is None:
        return None
    s = str(value).strip().replace(",", "")
    if not s:
        return None
    try:
        if s.endswith("亿"):
            return float(s[:-1])
        if s.endswith("万"):
            return float(s[:-1]) / 1e4
        return float(s) / 1e8
    except ValueError:
        return None


def national_team(ticker: str) -> list[dict]:
    """国家队主体在十大股东里的持股与环比变化（按最新报告期）。"""
    import akshare as ak

    code = _code(ticker)
    df = ak.stock_main_stock_holder(stock=code)
    if df is None or df.empty:
        return []

    df = df.copy()
    for col in ("股东名称", "持股比例", "截至日期"):
        if col not in df.columns:
            return []
    df["截止"] = df["截至日期"].astype(str)
    periods = sorted(df["截止"].unique(), reverse=True)
    if not periods:
        return []

    def rows_of(period: str) -> dict[str, float]:
        sub = df[df["截止"] == period]
        out = {}
        for _, r in sub.iterrows():
            name = str(r.get("股东名称") or "")
            if any(k in name for k in _NATIONAL_TEAM_KEYS):
                ratio = r.get("持股比例")
                try:
                    out[name] = float(ratio)
                except (TypeError, ValueError):
                    continue
        return out

    latest = rows_of(periods[0])
    prior = rows_of(periods[1]) if len(periods) > 1 else {}
    result = []
    for name, ratio in latest.items():
        prev = prior.get(name)
        result.append({
            "name": name,
            "period": periods[0],
            "ratio": ratio,
            "prev_ratio": prev,
            "delta": (ratio - prev) if prev is not None else None,
        })
    return result


def lhb_recent(ticker: str, days: int = 60) -> dict:
    """近 N 天龙虎榜上榜次数与机构席位净买（亿元）。"""
    import akshare as ak
    from datetime import datetime, timedelta

    code = _code(ticker)
    end = datetime.now()
    start = end - timedelta(days=days)
    df = ak.stock_lhb_detail_em(start_date=start.strftime("%Y%m%d"),
                               end_date=end.strftime("%Y%m%d"))
    if df is None or df.empty or "代码" not in df.columns:
        return {"count": 0, "rows": []}
    sub = df[df["代码"].astype(str).str.zfill(6) == code]
    if sub.empty:
        return {"count": 0, "rows": []}
    rows = []
    for _, r in sub.iterrows():
        net = r.get("龙虎榜净买额")
        try:
            net_yi = float(net) / 1e8
        except (TypeError, ValueError):
            net_yi = None
        rows.append({
            "date": str(r.get("上榜日"))[:10],
            "reason": str(r.get("解读") or "")[:40],
            "net": net_yi,
            "change_pct": r.get("涨跌幅"),
        })
    return {"count": len(rows), "rows": rows[-5:]}


def capital_flow_report(ticker: str, curr_date: str | None = None) -> str:
    """资金流 + 国家队 + 龙虎榜，并给出与量价的交叉判断。"""
    code = _code(ticker)
    lines = [f"## 资金流与国家队 — {ticker}"]
    ok_any = False

    # 一、当日资金流
    try:
        snap = ths_flow_snapshot()
        me = snap.get(code)
        if not me:
            raise NoMarketDataError(ticker, ticker, "not in 同花顺 fund-flow snapshot")
        inflow = _to_yi(me.get("inflow"))
        outflow = _to_yi(me.get("outflow"))
        net = _to_yi(me.get("net"))
        amount = _to_yi(me.get("amount"))
        nets = [v for v in (_to_yi(x.get("net")) for x in snap.values()) if v is not None]
        rank = (sum(1 for v in nets if v <= net) / len(nets) * 100) if nets and net is not None else None

        lines.append("### 一、当日资金流（同花顺全市场）")
        if net is not None and amount:
            ratio = net / amount * 100 if amount else None
            lines.append(f"- 净额 **{net:+.2f} 亿**（流入 {inflow:.2f} 亿 / 流出 {outflow:.2f} 亿），"
                         f"占成交额 **{ratio:+.1f}%**")
        else:
            lines.append(f"- 净额 {net} 亿")
        if rank is not None:
            lines.append(f"- 全市场净流入分位：**{rank:.0f}%**"
                         f"（{'高于' if rank >= 50 else '低于'}半数个股）")
        # 与量价交叉：方向一致才算真信号
        chg = me.get("change_pct")
        try:
            chg_v = float(str(chg).replace("%", ""))
        except (TypeError, ValueError):
            chg_v = None
        if net is not None and chg_v is not None:
            if net > 0 and chg_v > 0:
                judge = "价涨量增、资金净流入 → 承接有力，属健康上涨"
            elif net < 0 and chg_v > 0:
                judge = "价涨但资金净流出 → 拉高出货嫌疑，谨慎追高"
            elif net > 0 and chg_v < 0:
                judge = "价跌但资金净流入 → 有资金逆势承接，可能是错杀"
            else:
                judge = "价跌且资金净流出 → 双弱，暂无承接"
            lines.append(f"- **量价交叉**：{judge}")
        ok_any = True
    except Exception as exc:  # noqa: BLE001
        lines.append("### 一、当日资金流")
        lines.append(f"- 取数失败（{type(exc).__name__}），本项记为数据缺口。")
    lines.append("")

    # 二、国家队
    try:
        team = national_team(ticker)
        lines.append("### 二、国家队 / 长期资金持仓（十大股东，季度披露）")
        if team:
            for t in team:
                delta = t["delta"]
                arrow = ("增持" if delta and delta > 0 else "减持" if delta and delta < 0 else "持平")
                delta_txt = f"，较上期 {delta:+.2f}pp（{arrow}）" if delta is not None else ""
                lines.append(f"- {t['name']}：持股 **{t['ratio']:.2f}%**（{t['period']}）{delta_txt}")
            lines.append("- 说明：国家队持仓是季度数据，滞后于股价；它的意义在于**底仓是否稳定**，"
                         "不用于判断短线方向。")
        else:
            lines.append("- 十大股东中未见中央汇金/证金/社保/养老金等主体（可能未进入前十）。")
        ok_any = True
    except Exception as exc:  # noqa: BLE001
        lines.append("### 二、国家队 / 长期资金持仓")
        lines.append(f"- 取数失败（{type(exc).__name__}），本项记为数据缺口。")
    lines.append("")

    # 三、龙虎榜
    try:
        lhb = lhb_recent(ticker)
        lines.append("### 三、龙虎榜（近 60 天）")
        if lhb["count"]:
            lines.append(f"- 上榜 **{lhb['count']}** 次，最近几次：")
            for r in lhb["rows"]:
                net_txt = f"{r['net']:+.2f} 亿" if r["net"] is not None else "n/a"
                lines.append(f"  - {r['date']} {r['reason']}（净买 {net_txt}，当日涨跌 {r['change_pct']}）")
            lines.append("- 上榜多说明游资/机构参与度高，波动会放大；机构席位净买为正更可信。")
        else:
            lines.append("- 近 60 天未上榜（无异常大额买卖披露）。")
        ok_any = True
    except Exception as exc:  # noqa: BLE001
        lines.append("### 三、龙虎榜")
        lines.append(f"- 取数失败（{type(exc).__name__}），本项记为数据缺口。")

    if not ok_any:
        return f"DATA_UNAVAILABLE: 资金流与国家队数据全部取数失败（{ticker}）"
    lines.append("")
    lines.append("> 资金流为当日口径（同花顺），国家队为季度披露口径。两者时间尺度不同，"
                 "不要用国家队持仓解释当日涨跌。")
    return "\n".join(lines)
