"""Local Tick Stock Panel (Quant Terminal) as a data vendor.

The user runs a Tick Stock Panel on the host that has already ingested the whole
A-share market: daily and minute bars, plus a wide set of precomputed
indicators (MA/EMA/MACD/BOLL/KDJ/ATR/RSI/momentum/turnover/limit prices and
signal flags). It answers on ``http://host.docker.internal:3018/api`` with no
authentication, and reading it is a local call — unlike Eastmoney's push2, which
throttles our IP after a burst of quote requests.

So this vendor sits at the front of the A-share quote chain: panel first,
Eastmoney/Sina as fallback when the panel is not running (the user may close it)
or when the symbol is not a domestic one (the panel only holds A-shares).

Only ``get_stock_data`` is served here. Everything downstream — the verified
market snapshot, the indicator tools, the charts — is derived from that same
OHLCV series, so one honest source is enough.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.vendors.akshare.common import is_a_share, is_etf

logger = logging.getLogger(__name__)

_DEFAULT_BASE = "http://host.docker.internal:3018"
_TIMEOUT = 25


def base_url() -> str:
    """Panel base URL; override with ``TICKSTOCK_BASE_URL`` when it moves."""
    return os.getenv("TICKSTOCK_BASE_URL", _DEFAULT_BASE).rstrip("/")


def panel_symbol(ticker: str) -> str:
    """``600999`` -> ``600999.SH``; ``000338`` -> ``000338.SZ``; ``830799`` -> ``830799.BJ``."""
    raw = (ticker or "").strip().upper()
    if "." in raw:
        code, _, suffix = raw.partition(".")
        suffix = suffix.upper()
        if suffix in ("SH", "SZ", "BJ"):
            return f"{code}.{suffix}"
        raw = code
    code = "".join(ch for ch in raw if ch.isdigit())
    if len(code) != 6:
        raise NoMarketDataError(ticker, ticker, f"'{ticker}' is not a 6-digit A-share code")
    head = code[0]
    if head == "6":                     # 沪市主板/科创板
        return f"{code}.SH"
    if head in ("0", "3"):              # 深市主板/创业板
        return f"{code}.SZ"
    if head == "5":                     # 沪市 ETF/LOF（588170 科创半导体ETF）
        return f"{code}.SH"
    if head in ("1", "2"):              # 深市基金（159852 软件ETF）
        return f"{code}.SZ"
    # 4/8 开头是北交所
    return f"{code}.BJ"


def _get(path: str, params: dict) -> dict | list:
    url = f"{base_url()}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        # 4xx means the panel answered but refused this request: a data problem,
        # not a transport problem.
        if 400 <= exc.code < 500:
            raise NoMarketDataError(
                str(params.get("symbol", "")), str(params.get("symbol", "")),
                f"panel returned HTTP {exc.code} for {path}",
            ) from exc
        raise VendorUnavailableError(f"Tick Stock Panel HTTP {exc.code} on {path}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise VendorUnavailableError(
            f"Tick Stock Panel unreachable at {base_url()} ({type(exc).__name__}); "
            "is the panel running?"
        ) from exc


def _daily_rows(symbol: str, start_date: str, end_date: str) -> list[dict]:
    """Raw daily bars from the panel, oldest first, clipped to the window."""
    panel_sym = panel_symbol(symbol)
    payload = _get("/api/kline/daily", {
        "symbol": panel_sym,
        "start_date": start_date,
        "end_date": end_date,
    })
    rows = payload if isinstance(payload, list) else (payload.get("rows") or [])
    out = []
    for r in rows:
        date = (r.get("date") or "")[:10]
        if date and start_date <= date <= end_date:
            out.append(r)
    if not out:
        raise NoMarketDataError(
            symbol, panel_sym,
            f"panel has no daily bars for {panel_sym} between {start_date} and {end_date}",
        )
    return out


def daily_frame(symbol: str, start_date: str, end_date: str):
    """Daily bars as a Date-indexed DataFrame (the shape ``load_ohlcv`` wants)."""
    import pandas as pd

    rows = _daily_rows(symbol, start_date, end_date)
    df = pd.DataFrame([{
        "Date": r.get("date")[:10],
        "Open": r.get("open"),
        "High": r.get("high"),
        "Low": r.get("low"),
        "Close": r.get("close"),
        "Volume": r.get("volume"),
        "Amount": r.get("amount"),
        "Turnover": r.get("turnover_rate"),
    } for r in rows])
    df["Date"] = pd.to_datetime(df["Date"])
    return df.sort_values("Date").reset_index(drop=True)


def get_stock(symbol: str, start_date: str, end_date: str) -> str:
    """Daily OHLCV for an A-share/ETF, in the same CSV shape the pipeline expects.

    Dates are inclusive and validated up front; the panel returns rows oldest
    first, which is what the rest of the pipeline assumes.
    """
    datetime.strptime(start_date, "%Y-%m-%d")
    datetime.strptime(end_date, "%Y-%m-%d")

    if not (is_a_share(symbol) or is_etf(symbol)):
        # Not a domestic symbol: let the router fall through to yfinance rather
        # than pretend the panel has it.
        raise NoMarketDataError(
            symbol, symbol,
            f"'{symbol}' is not an A-share/ETF; the Tick Stock Panel only holds the A-share market",
        )

    panel_sym = panel_symbol(symbol)
    rows = _daily_rows(symbol, start_date, end_date)

    # The panel reports change_pct as a fraction (-0.0263) while the pipeline's
    # PctChange column is percent (-2.63), as AkShare reports it.
    def _pct(v):
        return "" if v is None else f"{float(v) * 100:.4f}"

    def _num(v):
        return "" if v is None else v

    lines = ["Date,Open,Close,High,Low,Volume,Amount,PctChange,Change,Turnover"]
    for r in rows:
        date = (r.get("date") or "")[:10]
        if not date or date < start_date or date > end_date:
            continue
        lines.append(",".join([
            date,
            str(_num(r.get("open"))), str(_num(r.get("close"))),
            str(_num(r.get("high"))), str(_num(r.get("low"))),
            str(_num(r.get("volume"))), str(_num(r.get("amount"))),
            _pct(r.get("change_pct")), str(_num(r.get("change_amount"))),
            str(_num(r.get("turnover_rate"))),
        ]))

    body = "\n".join(lines[1:])
    if not body:
        raise NoMarketDataError(
            symbol, panel_sym,
            f"panel returned bars outside {start_date}..{end_date} for {panel_sym}",
        )

    label = f"{panel_sym} (from {symbol})" if panel_sym.split(".")[0] != symbol.upper() else panel_sym
    header = f"# Stock data for {label} from {start_date} to {end_date}\n"
    header += f"# Total records: {len(body.splitlines())}\n"
    header += f"# Source: Tick Stock Panel ({base_url()})\n\n"
    return header + lines[0] + "\n" + body + "\n"


def market_snapshot() -> list[dict]:
    """The whole A-share snapshot in one call (~5k rows, with caps/turnover)."""
    payload = _get("/api/screener/market-snapshot", {})
    rows = payload if isinstance(payload, list) else (payload.get("rows") or [])
    return rows


# ---------------------------------------------------------------------------
# 预计算指标：直接读面板算好的值，不再用 OHLCV 重算
# ---------------------------------------------------------------------------
# 面板每根 bar 都带 60+ 个指标与信号位。重算一遍既浪费算力，又会因为窗口/
# 复权口径不同而与面板（用户自己看的那个界面）对不上——分析师引用的数字必须
# 和用户屏幕上一致，否则没法对话。
# 只保留判趋势/找买点必需的一组：列太多会把上下文吃满，反而看不清。
_INDICATOR_COLUMNS = (
    ("close", "收盘"), ("change_pct", "涨跌幅%"),
    ("ma5", "MA5"), ("ma20", "MA20"), ("ma60", "MA60"),
    ("macd_dif", "DIF"), ("macd_dea", "DEA"), ("macd_hist", "MACD柱"),
    ("rsi_14", "RSI14"), ("kdj_k", "K"), ("kdj_d", "D"),
    ("boll_upper", "BOLL上"), ("boll_lower", "BOLL下"),
    ("atr_14", "ATR14"), ("vol_ratio_5d", "量比"), ("turnover_rate", "换手%"),
)

# 信号位是布尔列，逐个打印会淹掉正文，只汇总"窗口内触发过哪些、最近一次是哪天"。
_SIGNAL_COLUMNS = (
    ("signal_ma_golden_5_20", "MA5上穿MA20(金叉)"),
    ("signal_ma_dead_5_20", "MA5下穿MA20(死叉)"),
    ("signal_ma_golden_20_60", "MA20上穿MA60(金叉)"),
    ("signal_ma5_breakout", "突破MA5"), ("signal_ma5_breakdown", "跌破MA5"),
    ("signal_ma10_breakout", "突破MA10"), ("signal_ma10_breakdown", "跌破MA10"),
    ("signal_ma20_breakout", "突破MA20"), ("signal_ma20_breakdown", "跌破MA20"),
    ("signal_macd_golden", "MACD金叉"), ("signal_macd_dead", "MACD死叉"),
    ("signal_boll_breakout_upper", "突破BOLL上轨"), ("signal_boll_breakdown_lower", "跌破BOLL下轨"),
    ("signal_n_day_high", "创N日新高"), ("signal_n_day_low", "创N日新低"),
    ("signal_volume_surge", "放量"),
    ("signal_limit_up", "涨停"), ("signal_limit_down", "跌停"),
    ("signal_broken_limit_up", "炸板"),
)


def _pos_in_range(row: dict) -> float:
    """Where the close sits inside its 60-day low..high, as 0-100."""
    lo, hi, close = row.get("low_60d"), row.get("high_60d"), row.get("close")
    try:
        if lo is None or hi is None or close is None or hi <= lo:
            return 50.0
        return max(0.0, min(100.0, (close - lo) / (hi - lo) * 100))
    except TypeError:
        return 50.0


def get_indicators(ticker: str, curr_date: str, look_back_days: int = 30) -> str:
    """Panel-precomputed indicators + signals for the window ending ``curr_date``.

    Returns a compact table of the latest ``look_back_days`` bars plus a summary
    of which signal flags fired in the window. Values are the panel's own, so
    they match what the user sees in the terminal.
    """
    from datetime import datetime as _dt, timedelta as _td

    end = _dt.strptime(curr_date, "%Y-%m-%d")
    start = end - _td(days=int(look_back_days) * 2 + 10)  # 自然日→交易日留余量
    rows = _daily_rows(ticker, start.strftime("%Y-%m-%d"), curr_date)
    rows = rows[-int(look_back_days):]
    if not rows:
        raise NoMarketDataError(ticker, ticker, f"no indicator rows up to {curr_date}")

    name = rows[-1].get("name") or ticker
    lines = [f"## {name}（{ticker}）面板预计算指标 — 截至 {rows[-1]['date']}"]
    lines.append("")
    header = "| 日期 | " + " | ".join(lbl for _, lbl in _INDICATOR_COLUMNS) + " |"
    sep = "|---" * (len(_INDICATOR_COLUMNS) + 1) + "|"
    lines.append(header)
    lines.append(sep)
    for r in rows:
        cells = []
        for key, _ in _INDICATOR_COLUMNS:
            v = r.get(key)
            if v is None:
                cells.append("-")
            elif isinstance(v, bool):
                cells.append("是" if v else "否")
            elif isinstance(v, (int, float)):
                if key == "change_pct":
                    cells.append(f"{v * 100:+.2f}")
                elif key in ("volume", "amount"):
                    cells.append(f"{v:,.0f}")
                elif abs(v) >= 1000:
                    cells.append(f"{v:,.0f}")
                else:
                    cells.append(f"{v:.2f}")
            else:
                cells.append(str(v))
        lines.append(f"| {r['date'][5:]} | " + " | ".join(cells) + " |")
    lines.append("")

    # 窗口内触发过的信号（附最近一次日期与次数）
    fired = []
    for key, label in _SIGNAL_COLUMNS:
        dates = [r["date"][5:] for r in rows if r.get(key) is True]
        if dates:
            fired.append(f"- **{label}**：{len(dates)} 次（最近 {dates[-1]}）")
    if fired:
        lines.append("### 窗口内触发的信号位")
        lines.extend(fired)
    else:
        lines.append("### 窗口内触发的信号位")
        lines.append("- 无（该窗口内未出现金叉/死叉/突破/放量/涨跌停等信号）")
    lines.append("")
    last = rows[-1]
    lines.append(
        f"**最新状态**：收盘 {last.get('close'):.2f}，涨跌幅 "
        f"{(last.get('change_pct') or 0) * 100:+.2f}%，"
        f"60 日区间 {last.get('low_60d'):.2f} ~ {last.get('high_60d'):.2f}"
        f"（当前位于区间 {_pos_in_range(last):.0f}%），"
        f"ATR14 {last.get('atr_14'):.2f}，连板 {last.get('consecutive_limit_ups')}。"
    )
    lines.append("")
    lines.append("> 数值直接取自 Tick Stock Panel 的入库指标（与终端界面一致），非本地重算。")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 全市场横截面：市场宽度 + 本标的分位 + 同业对比
# ---------------------------------------------------------------------------
# 面板一次调用就给全 A 5000+ 行的市值/成交/换手/量比，这让"横截面"变得很便宜。
# 单标的纵向看技术面容易失真——同样一根阴线，在全市场普跌里是噪声，在普涨里
# 是独立走弱。这一层就是给分析师一个"相对位置"的锚。

# 同业映射：只收录我能确定代码的标的。宁可缺，不可写错——错误的同行会把
# 相对强弱结论整个带偏。未收录的标的只做全市场分位，不做同业对比。
PEER_MAP: dict[str, tuple[str, ...]] = {
    "002371": ("688012", "688072", "688037", "300604"),   # 半导体设备
    "002475": ("002241", "300433", "601138"),             # 消费电子/果链
    "300308": ("300502", "002281", "600487"),             # 光模块/光通信
    "688008": ("603986", "688123", "300661"),             # 存储接口/模拟芯片
    "603259": ("300759", "002821", "603127"),             # CXO
    "603186": ("600183", "002916", "300476"),             # 覆铜板/PCB
    "600999": ("600030", "601688", "601211"),             # 券商
    "601939": ("601398", "601288", "600036"),             # 银行
    "600900": ("600886", "600025", "601985"),             # 电力
    "000338": ("000951", "000425", "600031"),             # 重卡/工程机械
}

_PEER_GROUP_NAME = {
    "002371": "半导体设备", "002475": "消费电子/果链", "300308": "光模块/光通信",
    "688008": "存储接口/模拟芯片", "603259": "CXO", "603186": "覆铜板/PCB",
    "600999": "券商", "601939": "银行", "600900": "电力", "000338": "重卡/工程机械",
}

_snapshot_cache: dict[str, object] = {}
_SNAPSHOT_TTL = 60.0  # 盘中 1 分钟内复用，避免同一次分析里反复拉 5000 行


def snapshot_cached() -> list[dict]:
    """Whole-market snapshot, reused for ``_SNAPSHOT_TTL`` seconds."""
    import time

    now = time.time()
    hit = _snapshot_cache.get("data")
    if hit is not None and now - float(_snapshot_cache.get("ts") or 0) < _SNAPSHOT_TTL:
        return hit  # type: ignore[return-value]
    rows = market_snapshot()
    _snapshot_cache["data"] = rows
    _snapshot_cache["ts"] = now
    return rows


def _pct_rank(values: list[float], v: float) -> float:
    """Percentage of ``values`` at or below ``v`` (0-100)."""
    if not values:
        return 0.0
    below = sum(1 for x in values if x <= v)
    return below / len(values) * 100


def cross_section(ticker: str, as_of: str | None = None) -> str:
    """Market breadth + the ticker's percentile + peer comparison."""
    rows = snapshot_cached()
    if not rows:
        raise NoMarketDataError(ticker, ticker, "panel market snapshot is empty")

    key = panel_symbol(ticker)
    me = next((r for r in rows if r.get("symbol") == key), None)
    if me is None:
        raise NoMarketDataError(ticker, key, f"{key} is not in the panel's market snapshot")

    chg = [r.get("change_pct") or 0.0 for r in rows]
    ups = sum(1 for c in chg if c > 0)
    downs = sum(1 for c in chg if c < 0)
    flats = len(chg) - ups - downs
    # 涨停/跌停按涨跌幅近似（A股 10%/20% 两档都有），宁可用阈值也不用状态位
    limit_up = sum(1 for c in chg if c >= 0.095)
    limit_down = sum(1 for c in chg if c <= -0.095)
    big_up = sum(1 for c in chg if c >= 0.05)
    big_down = sum(1 for c in chg if c <= -0.05)
    total_amount = sum((r.get("amount") or 0) for r in rows) / 1e8
    med = sorted(chg)[len(chg) // 2] * 100

    lines = [f"## 全市场横截面 — {me.get('name') or ticker}（{key}）"]
    if as_of:
        lines.append(f"数据时点：{as_of}（panel snapshot 为最新交易日）")
    lines.append("")
    lines.append("### 一、市场宽度（情绪温度）")
    lines.append(f"- 全市场 {len(rows)} 只：**上涨 {ups} / 下跌 {downs} / 平盘 {flats}**"
                 f"（上涨占比 {ups / len(rows) * 100:.0f}%）")
    lines.append(f"- 涨停 {limit_up} 家 / 跌停 {limit_down} 家；涨幅≥5% 共 {big_up} 家，跌幅≥5% 共 {big_down} 家")
    lines.append(f"- 全市场成交额合计 **{total_amount:,.0f} 亿元**；个股涨跌幅中位数 **{med:+.2f}%**")
    breadth = "普涨" if ups / len(rows) > 0.65 else "普跌" if downs / len(rows) > 0.65 else "结构分化"
    lines.append(f"- 判定：**{breadth}**。单只个股的涨跌要放到这个背景里读——"
                 f"{'普涨中的下跌更可能是独立走弱' if breadth == '普涨' else '普跌中的下跌多为系统性回撤' if breadth == '普跌' else '分化市里个股走势的独立信息量最大'}。")
    lines.append("")

    lines.append("### 二、本标的全市场分位")
    lines.append("| 指标 | 本标的 | 全市场分位 | 含义 |")
    lines.append("|---|---:|---:|---|")
    checks = [
        ("market_cap", "总市值", lambda v: f"{v / 1e8:,.0f} 亿", 1e8),
        ("amount", "成交额", lambda v: f"{v / 1e8:,.2f} 亿", 1e8),
        ("turnover_rate", "换手率", lambda v: f"{v:.2f}%", 1.0),
        ("vol_ratio_5d", "量比(5日)", lambda v: f"{v:.2f}", 1.0),
        ("change_pct", "涨跌幅", lambda v: f"{v * 100:+.2f}%", 1.0),
    ]
    for field, label, fmt, scale in checks:
        v = me.get(field)
        if v is None:
            continue
        vals = [r.get(field) for r in rows if isinstance(r.get(field), (int, float))]
        rank = _pct_rank(vals, v)
        level = "极高" if rank >= 90 else "偏高" if rank >= 70 else "偏低" if rank <= 30 else "中等"
        lines.append(f"| {label} | {fmt(v)} | {rank:.0f}% | {level} |")
    lines.append("")

    # 同业优先用面板的同花顺行业（动态、覆盖全市场），取不到时才回落到手写的
    # PEER_MAP。动态分组的好处是新增持仓不用维护映射表，且口径与用户终端一致。
    ind_map = industry_map()
    industry = ind_map.get(key, "")
    dyn_peers = []
    industry_all: list[dict] = []
    if industry:
        industry_all = [r for r in rows
                        if r.get("symbol") != key and ind_map.get(r.get("symbol")) == industry]
        dyn_peers = sorted(industry_all, key=lambda r: r.get("market_cap") or 0, reverse=True)[:5]
    if dyn_peers or PEER_MAP.get((ticker or "").strip().upper()) or PEER_MAP.get(ticker):
        static = PEER_MAP.get((ticker or "").strip().upper()) or PEER_MAP.get(ticker) or ()
        group = industry or _PEER_GROUP_NAME.get((ticker or "").strip().upper(), "同业")
        lines.append(f"### 三、同业对比（{group}）")
        lines.append("| 标的 | 名称 | 收盘 | 涨跌幅 | 换手% | 量比 | 总市值(亿) |")
        lines.append("|---|---|---:|---:|---:|---:|---:|")
        rows_peer = [me]
        if dyn_peers:
            rows_peer.extend(dyn_peers)
        else:
            for p in static:
                sym = panel_symbol(p)
                match = next((r for r in rows if r.get("symbol") == sym), None)
                if match:
                    rows_peer.append(match)
        for r in rows_peer:
            mark = "**" if r is me else ""
            lines.append(
                f"| {mark}{r.get('symbol')}{mark} | {r.get('name') or '-'} | "
                f"{(r.get('close') or 0):.2f} | {((r.get('change_pct') or 0) * 100):+.2f}% | "
                f"{(r.get('turnover_rate') or 0):.2f} | {(r.get('vol_ratio_5d') or 0):.2f} | "
                f"{((r.get('market_cap') or 0) / 1e8):,.0f} |"
            )
        # 排名按**整个行业**算，而不是只按表里展示的那几只——展示的是市值最大的
        # 可比公司，用它们算排名会高估相对强弱。
        rank_pool = [me] + (industry_all if industry_all else rows_peer[1:])
        if len(rank_pool) > 1:
            by_chg = sorted(rank_pool, key=lambda r: r.get("change_pct") or 0, reverse=True)
            pos = next(i for i, r in enumerate(by_chg) if r is me) + 1
            by_to = sorted(rank_pool, key=lambda r: r.get("turnover_rate") or 0, reverse=True)
            pos_to = next(i for i, r in enumerate(by_to) if r is me) + 1
            lines.append("")
            lines.append(f"- 同业 {len(rank_pool)} 只中：**涨跌幅排第 {pos}**，"
                         f"**换手率排第 {pos_to}**。")
        else:
            lines.append("")
            lines.append(f"- 同业 {len(rows_peer)} 只中：**涨跌幅排第 "
                         f"{next(i for i, r in enumerate(sorted(rows_peer, key=lambda r: r.get('change_pct') or 0, reverse=True)) if r is me) + 1}**。")
    else:
        lines.append("### 三、同业对比")
        lines.append("- 未收录该标的的同业映射，本节跳过（仅做全市场分位）。")
    lines.append("")
    lines.append("> 数据来自 Tick Stock Panel 全市场快照（单次调用返回全 A），非逐只查询。")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 因子分析：因子暴露 + 全市场/同业分位 + 市场状态
# ---------------------------------------------------------------------------
# 面板的因子库有 991 个（动量/波动/量价/财务/Barra/新闻情绪/分析师预期/Tick…），
# 但 /api/backtest/factor/run 要载入全市场因子面板，单次 90 秒起步——放在分析
# 流程里会把整轮拖死。所以这里走快路径：
#   · 标的自身的多周期因子 → 面板日线（每根 bar 已带）
#   · 横截面分位 → 全市场快照（一次 5000+ 行，60 秒缓存）
#   · 同业分组 → ext-data 的同花顺行业（5582 只，10 分钟缓存）
#   · 市场环境 → /api/regime/latest（情绪/封板/连板梯队）
# 需要 991 因子里某一个的 IC/分组收益时，再单独跑回测接口，不放进自动流程。

_industry_cache: dict[str, object] = {}
_INDUSTRY_TTL = 600.0


def industry_map(force: bool = False) -> dict[str, str]:
    """symbol -> 同花顺行业（如 ``半导体-集成电路制造``），缓存 10 分钟。"""
    import time

    now = time.time()
    hit = _industry_cache.get("data")
    if hit is not None and not force and now - float(_industry_cache.get("ts") or 0) < _INDUSTRY_TTL:
        return hit  # type: ignore[return-value]
    try:
        payload = _get("/api/ext-data/ext_hy_ths/rows", {"limit": 8000})
        rows = payload if isinstance(payload, list) else (payload.get("rows") or [])
        mapping = {}
        for r in rows:
            sym = r.get("symbol")
            ind = r.get("所属同花顺行业")
            if sym and ind:
                mapping[sym] = ind
        if mapping:
            _industry_cache["data"] = mapping
            _industry_cache["ts"] = now
        return mapping
    except Exception as exc:  # noqa: BLE001 - 行业数据缺失只影响同业对比
        logger.debug("industry map unavailable: %s", exc)
        return {}


def regime_latest() -> dict:
    """Panel's market-regime snapshot (emotion score, seal rate, ladders)."""
    try:
        payload = _get("/api/regime/latest", {})
        return payload.get("row") or {} if isinstance(payload, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.debug("regime unavailable: %s", exc)
        return {}


def _pct(values: list[float], v: float) -> float:
    if not values:
        return 0.0
    return sum(1 for x in values if x <= v) / len(values) * 100


def factor_analysis(ticker: str, as_of: str | None = None, look_back_days: int = 60) -> str:
    """Factor exposure + cross-sectional percentiles + market regime."""
    from datetime import datetime as _dt, timedelta as _td

    rows = snapshot_cached()
    if not rows:
        raise NoMarketDataError(ticker, ticker, "panel market snapshot is empty")
    key = panel_symbol(ticker)
    me = next((r for r in rows if r.get("symbol") == key), None)
    if me is None:
        raise NoMarketDataError(ticker, key, f"{key} is not in the panel's market snapshot")

    ind_map = industry_map()
    industry = ind_map.get(key, "")
    peers = [r for r in rows if industry and ind_map.get(r.get("symbol")) == industry]

    lines = [f"## 因子分析 — {me.get('name') or ticker}（{key}）"]
    if industry:
        lines.append(f"同花顺行业：**{industry}**（同业 {len(peers)} 只）")
    else:
        lines.append("行业归属：未取到（ext-data 行业表不可用），同业对比降级为全市场。")
    lines.append("")

    # 一、多周期因子暴露（来自面板日线，每根 bar 自带）
    end = _dt.strptime(as_of, "%Y-%m-%d") if as_of else _dt.today()
    start = end - _td(days=look_back_days * 2 + 10)
    try:
        bars = _daily_rows(ticker, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
        last = bars[-1] if bars else {}
    except NoMarketDataError:
        bars, last = [], {}

    lines.append("### 一、多周期因子暴露（面板日线计算）")
    lines.append("| 因子 | 取值 | 读法 |")
    lines.append("|---|---:|---|")
    exposure = [
        ("5日动量", last.get("momentum_5d"), "短周期情绪，>0 表示近一周资金仍偏多"),
        ("20日动量", last.get("momentum_20d"), "月度趋势，与 60 日同向说明趋势稳定"),
        ("60日动量", last.get("momentum_60d"), "季度趋势，反向说明可能是反弹而非反转"),
        ("MA20乖离", (last.get("close") / last["ma20"] - 1) if last.get("ma20") else None, ">8% 偏热易回抽，<-8% 偏冷易反抽"),
        ("60日位置", _pos_in_range(last) if last else None, "0%=60日最低，100%=60日最高"),
        ("ATR/价格", (last.get("atr_14") / last["close"]) if last.get("close") else None, "波动率，越高止损要越宽、仓位要越小"),
        ("年化波动(20日)", last.get("annual_vol_20d"), "波动率因子，与市场分位对照看是否异常放大"),
    ]
    for label, v, note in exposure:
        if v is None:
            continue
        if label == "60日位置":
            lines.append(f"| {label} | {v:.0f}% | {note} |")
        elif label in ("ATR/价格", "年化波动(20日)"):
            lines.append(f"| {label} | {v * 100:.1f}% | {note} |")
        else:
            lines.append(f"| {label} | {v * 100:+.1f}% | {note} |")
    lines.append("")

    # 二、横截面分位（全市场 + 同业）——只有快照里有的因子能算全市场分位
    lines.append("### 二、横截面分位（全市场 / 同业）")
    lines.append("| 因子 | 本标的 | 全市场分位 | 同业中位数 | 同业分位 |")
    lines.append("|---|---:|---:|---:|---:|")
    cs_factors = [
        ("change_pct", "当日涨跌幅", lambda v: f"{v * 100:+.2f}%"),
        ("turnover_rate", "换手率(流动性)", lambda v: f"{v:.2f}%"),
        ("vol_ratio_5d", "量比(量能)", lambda v: f"{v:.2f}"),
        ("market_cap", "总市值(规模)", lambda v: f"{v / 1e8:,.0f}亿"),
        ("amount", "成交额(流动性)", lambda v: f"{v / 1e8:,.2f}亿"),
    ]
    for field, label, fmt in cs_factors:
        v = me.get(field)
        if v is None:
            continue
        all_vals = [r.get(field) for r in rows if isinstance(r.get(field), (int, float))]
        peer_vals = [r.get(field) for r in peers if isinstance(r.get(field), (int, float))]
        med = sorted(peer_vals)[len(peer_vals) // 2] if peer_vals else None
        lines.append(
            f"| {label} | {fmt(v)} | {_pct(all_vals, v):.0f}% | "
            f"{fmt(med) if med is not None else '-'} | "
            f"{_pct(peer_vals, v):.0f}%" if peer_vals else
            f"| {label} | {fmt(v)} | {_pct(all_vals, v):.0f}% | - | - |"
        )
    lines.append("")

    # 三、同业分化度：这只票的涨跌有多少是行业共性
    if peers and industry:
        peer_chg = [r.get("change_pct") or 0.0 for r in peers]
        peer_med = sorted(peer_chg)[len(peer_chg) // 2] * 100
        mine = (me.get("change_pct") or 0.0) * 100
        better = sum(1 for c in peer_chg if c * 100 > mine)
        rank = better + 1
        diff = mine - peer_med
        lines.append("### 三、同业分化度")
        lines.append(f"- 同业 {len(peers)} 只今日涨跌幅中位数 **{peer_med:+.2f}%**，本标的 **{mine:+.2f}%**，"
                     f"{'强于' if diff >= 0 else '弱于'}同业中位数 **{abs(diff):.2f}pp**")
        lines.append(f"- 同业排名：**{rank}/{len(peers)}**（1=最强，{len(peers)}=最弱）")
        spread = (max(peer_chg) - min(peer_chg)) * 100
        lines.append(f"- 行业内部离散度 {spread:.2f}pp —— "
                     f"{'分化大，个股逻辑重于行业 beta' if spread > 6 else '分化小，跟随行业整体波动'}")
        lines.append("")

    # 四、市场环境
    reg = regime_latest()
    if reg:
        lines.append("### 四、市场环境（面板 regime）")
        lines.append(f"- 状态：**{reg.get('state')}**，情绪评分 **{reg.get('score')}**"
                     f"（利润分 {reg.get('profit_score')} / 投机分 {reg.get('speculation_score')} / "
                     f"韧性分 {reg.get('resilience_score')} / 趋势分 {reg.get('trend_score')}）")
        lines.append(f"- 涨停 {reg.get('limit_up')} / 跌停 {reg.get('limit_down')}，"
                     f"炸板 {reg.get('broken_limit')}，封板率 {(reg.get('seal_rate') or 0) * 100:.1f}%，"
                     f"最高连板 {reg.get('max_consecutive')}，首板 {reg.get('first_board')}")
        lines.append(f"- 上涨 {reg.get('up_count')} / 下跌 {reg.get('down_count')}，"
                     f"站上 MA20 的占比 {(reg.get('above_ma20_pct') or 0) * 100:.1f}%，"
                     f"成交额 {(reg.get('total_amount') or 0) / 1e8:,.0f} 亿")
        seal = (reg.get("seal_rate") or 0) * 100
        if seal >= 60:
            style = "情绪偏热、追涨有效，动量/量能因子占优，但要注意高位分歧"
        elif seal >= 35:
            style = "情绪中性，量价与趋势因子有效性一般，重个股逻辑"
        else:
            style = "封板率低、炸板多，追高胜率差；低波动、低换手、有业绩支撑的因子更抗跌"
        lines.append(f"- 因子风格倾向：**{style}**")
        lines.append("")

    lines.append("### 五、面板因子库")
    lines.append("- 面板共 **991 个因子 / 34 个分组**（动量、均线偏离、超买超卖、趋势、波动率、"
                 "量价、价格位置、收益形态、流动性、涨停基因、财务、规模、K线形态，以及外部因子："
                 "Barra 风格与行业、分析师评级/目标价/一致预期、新闻情绪、集合竞价、逐笔成交与委托、"
                 "Tick 深度、Transformer、量价 RL 等）。")
    lines.append("- 需要其中某个因子的全市场 IC/分组收益时，可以让系统单独跑回测（较慢，约 1~2 分钟），"
                 "不适合每轮自动执行；本工具给的是**因子暴露 + 横截面分位 + 环境判断**的快路径。")
    lines.append("")
    lines.append("> 分位含义：90% 表示该值高于全市场 90% 的股票。市值/成交额分位高说明是大票，"
                 "换手/量比分位高说明资金关注度异常。")
    return "\n".join(lines)
