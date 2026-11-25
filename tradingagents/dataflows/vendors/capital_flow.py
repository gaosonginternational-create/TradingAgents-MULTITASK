"""Capital flow & national-team positioning for A-shares.

The user's Tick Stock Panel holds quotes, bars, indicators and a factor library,
but **no money-flow or holder data** (checked: 68 endpoints, no flow/北向/龙虎榜
route, and the frontend bundle has no such strings). So this module fills the gap
from sources that don't go through Eastmoney's push2 — which our IP gets
rate-limited on after a burst:

  · 当日资金流：同花顺全市场资金流（一次取回 5000+ 只，含流入/流出/净额/成交额）
  · 国家队（代理指标）：宽基 ETF 的**份额变化 → 净申购额**。ETF 分两层交易，
    二级市场买卖只换手不改份额，只有一级市场申赎才改总份额；汇金等大额申购
    宽基 ETF 会直接把份额顶上去，所以"大跌日头部沪深300ETF 份额跳增"是市场
    公认的托底观察窗口。注意：份额≠持仓，套利盘/保险/公募也能申购，
    只能在季报/年报的前十大持有人里确认归属，所以这里只做**推断**。
    深市 ETF（159919/159915）无公开日频份额接口，故只用沪市宽基。
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
# 沪市头部宽基 ETF：公开报道里"国家队买宽基"反复点名的主要就是这几只。
# 沪深300 四只（510300 规模最大、对政策资金最敏感）+ 上证50/中证500/中证1000/科创50。
BROAD_ETFS: dict[str, tuple[str, str]] = {
    "510300": ("沪深300ETF(华泰柏瑞)", "hs300"),
    "510310": ("沪深300ETF(易方达)", "hs300"),
    "510330": ("沪深300ETF(华夏)", "hs300"),
    "510360": ("沪深300ETF(广发)", "hs300"),
    "510050": ("上证50ETF", "sse50"),
    "510500": ("中证500ETF", "zz500"),
    "512100": ("中证1000ETF", "zz1000"),
    "588000": ("科创50ETF", "kc50"),
}

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


def _share_cache_path(date_str: str) -> str:
    from tradingagents.dataflows.config import get_config

    d = os.path.join(get_config()["data_cache_dir"], "etf_shares")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{date_str}.csv")


def sse_shares(date_str: str) -> dict[str, float]:
    """上交所 ETF 当日总份额（份），按日期落盘缓存——交易所每天才更新一次。"""
    path = _share_cache_path(date_str)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return {line.split(",")[0]: float(line.split(",")[1])
                        for line in fh.read().splitlines() if "," in line}
        except (OSError, ValueError):
            pass
    import akshare as ak

    df = ak.fund_etf_scale_sse(date=date_str.replace("-", ""))
    out: dict[str, float] = {}
    for _, r in df.iterrows():
        code = str(r.get("基金代码", "")).zfill(6)
        try:
            out[code] = float(r.get("基金份额"))
        except (TypeError, ValueError):
            continue
    try:
        with open(path, "w", encoding="utf-8") as fh:
            for k, v in out.items():
                fh.write(f"{k},{v}\n")
    except OSError:
        pass
    return out


def etf_nav() -> dict[str, float]:
    """ETF 单位净值（同花顺），用于把份额变化换算成金额。"""
    now = time.time()
    hit = _flow_cache.get("nav")
    if hit is not None and now - float(_flow_cache.get("nav_ts") or 0) < 600:
        return hit  # type: ignore[return-value]
    import akshare as ak

    df = ak.fund_etf_spot_ths()
    out: dict[str, float] = {}
    for _, r in df.iterrows():
        code = str(r.get("基金代码", "")).zfill(6)
        try:
            out[code] = float(r.get("当前-单位净值") or r.get("最新-单位净值"))
        except (TypeError, ValueError):
            continue
    _flow_cache["nav"] = out
    _flow_cache["nav_ts"] = now
    return out


def _trading_dates_back(n: int) -> list[str]:
    """最近 n 个工作日（含今天），由近及远——份额只在该日收盘后才有。"""
    from datetime import datetime, timedelta

    out, d = [], datetime.now()
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.strftime("%Y-%m-%d"))
        d -= timedelta(days=1)
    return out


def etf_flow(days: int = 5) -> dict:
    """头部宽基 ETF 的份额变化与净申购（亿元）。"""
    dates = _trading_dates_back(days + 2)
    series: list[tuple[str, dict[str, float]]] = []
    for d in dates:
        try:
            series.append((d, sse_shares(d)))
        except Exception:  # noqa: BLE001 - a missing day just shortens the window
            continue
        if len(series) >= days + 1:
            break
    if len(series) < 2:
        raise NoMarketDataError("ETF", "ETF", "上交所份额数据不足两天，无法算净申购")

    navs = etf_nav()
    today_date, today = series[0]
    prev_date, prev = series[1]
    per_etf = {}
    for code, (name, group) in BROAD_ETFS.items():
        t, p = today.get(code), prev.get(code)
        if t is None or p is None:
            continue
        d_shares = (t - p) / 1e8                  # 亿份
        nav = navs.get(code) or 0
        per_etf[code] = {
            "name": name, "group": group,
            "shares": t / 1e8, "d_shares": d_shares,
            "nav": nav, "net": d_shares * nav,     # 亿元
            "scale": t * nav / 1e8,
        }

    # 多日累计（沪深300 四只合并）
    cum = None
    if len(series) >= 2:
        oldest_date, oldest = series[-1]
        cum = {"from": oldest_date, "to": today_date, "hs300": 0.0}
        for code, (name, group) in BROAD_ETFS.items():
            if group != "hs300":
                continue
            a, b = today.get(code), oldest.get(code)
            if a is None or b is None:
                continue
            cum["hs300"] += (a - b) / 1e8 * (navs.get(code) or 0)
    return {"today": today_date, "prev": prev_date, "per_etf": per_etf, "cum": cum}


def national_team(ticker: str) -> list[dict]:
    """季度基线：国家队主体在十大股东里的持股与环比变化。

    半年报/年报才披露持有人姓名，季报只有"机构1/机构2"，所以这一项只能当**基线**，
    用来确认份额变化的归属，不能用来解释任何一天的涨跌。
    """
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

    # 二、宽基 ETF 申赎（国家队代理指标）
    try:
        fl = etf_flow(5)
        per = fl["per_etf"]
        lines.append("### 二、宽基ETF申赎（国家队代理指标）")
        lines.append(f"口径：{fl['today']} 相对 {fl['prev']} 的交易所披露份额变化 × 当日单位净值。")
        from datetime import datetime as _now_dt
        if fl["today"] != _now_dt.now().strftime("%Y-%m-%d"):
            lines.append(f"> 注：{_now_dt.now():%Y-%m-%d} 的份额交易所尚未披露（收盘后才更新），"
                         f"以上为最新可得交易日 **{fl['today']}** 的数据。")
        lines.append("")
        hs300 = {k: v for k, v in per.items() if v["group"] == "hs300"}
        if hs300:
            net = sum(v["net"] for v in hs300.values())
            scale = sum(v["scale"] for v in hs300.values())
            lines.append(f"**沪深300ETF（4 只合计）**：净申购 **{net:+.2f} 亿元**"
                         f"（合计规模 {scale:,.0f} 亿，占比 {net / scale * 100:+.2f}%）")
            lines.append("")
            lines.append("| ETF | 份额(亿份) | 当日份额变化(亿份) | 净申购(亿元) | 规模(亿元) |")
            lines.append("|---|---:|---:|---:|---:|")
            for code, v in sorted(hs300.items()):
                lines.append(f"| {v['name']} | {v['shares']:,.1f} | {v['d_shares']:+.2f} | "
                             f"{v['net']:+.2f} | {v['scale']:,.0f} |")
            lines.append("")
        others = {k: v for k, v in per.items() if v["group"] != "hs300"}
        if others:
            lines.append("其他宽基：")
            for code, v in sorted(others.items()):
                lines.append(f"- {v['name']}：份额变化 {v['d_shares']:+.2f} 亿份 → "
                             f"净申赎 {v['net']:+.2f} 亿元")
            lines.append("")

        cum = fl.get("cum")
        if cum:
            lines.append(f"- 沪深300ETF 四只 **{cum['from']} → {cum['to']} 累计净申购 "
                         f"{cum['hs300']:+.1f} 亿元**（累计口径，跨周末按可得交易日拼接）")

        # 与当日行情交叉：大跌日份额跳增 = 市场公认的托底信号
        try:
            from tradingagents.dataflows.vendors.tickstock import daily_frame
            from datetime import datetime as _dt, timedelta as _td

            end = _dt.strptime(fl["today"], "%Y-%m-%d")
            df = daily_frame("510300", (end - _td(days=12)).strftime("%Y-%m-%d"),
                             end.strftime("%Y-%m-%d"))
            chg = float(df.iloc[-1]["Close"] / df.iloc[-2]["Close"] - 1) * 100
            hs_net = sum(v["net"] for v in hs300.values()) if hs300 else 0
            if chg < -1 and hs_net > 5:
                judge = (f"沪深300 当日 **{chg:+.2f}%** 且头部宽基净申购 **{hs_net:+.1f} 亿** "
                         "→ 典型的**大跌日承接**，市场通常解读为托底资金出手")
            elif chg > 1 and hs_net > 5:
                judge = f"沪深300 当日 **{chg:+.2f}%** 且净申购 **{hs_net:+.1f} 亿** → 顺势加仓，非托底动作"
            elif hs_net < -5:
                judge = f"沪深300 当日 **{chg:+.2f}%** 但头部宽基净赎回 **{hs_net:+.1f} 亿** → 一级市场在减仓"
            else:
                judge = f"沪深300 当日 **{chg:+.2f}%**，宽基申赎 **{hs_net:+.1f} 亿**，未见明显异动"
            lines.append(f"- **行情交叉**：{judge}")
        except Exception:  # noqa: BLE001 - 行情取不到就只报份额
            pass
        lines.append("")
        lines.append("> ⚠️ 份额变化 ≠ 国家队持仓：套利盘、保险、公募 FOF 或资金在 ETF 间搬家"
                     "都会造成份额变动。只有半年报/年报的前十大持有人能确认归属，"
                     "所以这里只能做**推断**，不能当实锤。深市 ETF（159919/159915）"
                     "无公开日频份额接口，未纳入。")
        ok_any = True
    except Exception as exc:  # noqa: BLE001
        lines.append("### 二、宽基ETF申赎（国家队代理指标）")
        lines.append(f"- 取数失败（{type(exc).__name__}: {exc}），本项记为数据缺口。")
    lines.append("")

    # 三、季度基线（仅用于确认归属，解释不了当日涨跌）
    try:
        team = national_team(ticker)
        if team:
            lines.append("### 三、季度基线（十大股东，仅供归属确认）")
            for t in team:
                delta = t["delta"]
                arrow = ("增持" if delta and delta > 0 else "减持" if delta and delta < 0 else "持平")
                delta_txt = f"，较上期 {delta:+.2f}pp（{arrow}）" if delta is not None else ""
                lines.append(f"- {t['name']}：持股 **{t['ratio']:.2f}%**（{t['period']}）{delta_txt}")
            lines.append("- 这是季报/年报口径，滞后 1~3 个月，**不能**用来解释任何一天的涨跌；"
                         "只用来判断国家队在这只票上的底仓是否稳定（通常也不是通过个股直接买）。")
            lines.append("")
    except Exception:  # noqa: BLE001 - 季度数据缺失不影响主结论
        pass

    # 四、龙虎榜
    try:
        lhb = lhb_recent(ticker)
        lines.append("### 四、龙虎榜（近 60 天）")
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
        lines.append("### 四、龙虎榜")
        lines.append(f"- 取数失败（{type(exc).__name__}），本项记为数据缺口。")

    if not ok_any:
        return f"DATA_UNAVAILABLE: 资金流与国家队数据全部取数失败（{ticker}）"
    lines.append("")
    lines.append("> 资金流为当日口径（同花顺），国家队为季度披露口径。两者时间尺度不同，"
                 "不要用国家队持仓解释当日涨跌。")
    return "\n".join(lines)
