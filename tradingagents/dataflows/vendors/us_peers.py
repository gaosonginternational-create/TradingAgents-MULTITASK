"""Cross-section for non-A-share tickers (US / HK / KR listed).

The Tick Stock Panel only holds the A-share market, so every panel tool returns
DATA_UNAVAILABLE for names like SKHY or NVDA — and the report loses the one
thing that makes a single-stock view trustworthy: whether the move is
idiosyncratic or just the sector moving.

This module fills that gap with Yahoo data: a curated peer set per ticker, one
batched price download, and the same relative-strength framing the A-share tool
gives (today's move, 5/20-day momentum, volume versus its own average, rank
among peers).

Peer sets are deliberately small and only include comparisons we are confident
about — a wrong peer poisons the relative-strength conclusion more than a
missing one.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 只收录有把握的同业。存储链：SKHY(SK海力士ADR)/MU/SNDK/三星/海力士韩股；
# 算力链：NVDA/AMD/AVGO/MRVL/INTC；光通信：LITE/GLW/COHR；大科技：AAPL/META。
US_PEERS: dict[str, tuple[str, ...]] = {
    "SKHY": ("MU", "SNDK", "000660.KS", "005930.KS"),
    "MU": ("SKHY", "SNDK", "000660.KS"),
    "SNDK": ("MU", "SKHY"),
    "NVDA": ("AMD", "AVGO", "MRVL", "INTC"),
    "AMD": ("NVDA", "INTC", "MRVL"),
    "MRVL": ("AVGO", "NVDA", "AMD"),
    "INTC": ("AMD", "NVDA", "TSM"),
    "AVGO": ("NVDA", "MRVL", "QCOM"),
    "LITE": ("GLW", "COHR", "AAOI"),
    "GLW": ("LITE", "COHR"),
    "AAPL": ("MSFT", "GOOGL", "META"),
    "META": ("GOOGL", "SNAP", "PINS"),
    "SOXX": ("SMH", "IGV"),
}

_NAME = {
    "SKHY": "SK海力士(ADR)", "MU": "美光", "SNDK": "闪迪", "000660.KS": "SK海力士(韩)",
    "005930.KS": "三星电子", "NVDA": "英伟达", "AMD": "超威半导体", "AVGO": "博通",
    "MRVL": "迈威尔", "INTC": "英特尔", "TSM": "台积电", "QCOM": "高通",
    "LITE": "Lumentum", "GLW": "康宁", "COHR": "相干", "AAOI": "应用光电",
    "AAPL": "苹果", "MSFT": "微软", "GOOGL": "谷歌", "META": "Meta",
    "SNAP": "Snap", "PINS": "Pinterest", "SOXX": "费城半导体ETF", "SMH": "半导体ETF",
    "IGV": "软件ETF",
}


def _stats(hist) -> dict | None:
    import pandas as pd

    if hist is None or len(hist) < 21:
        return None
    close = hist["Close"].dropna()
    vol = hist["Volume"].dropna()
    if len(close) < 21:
        return None
    last, prev = float(close.iloc[-1]), float(close.iloc[-2])
    return {
        "last": last,
        "chg": (last / prev - 1) * 100 if prev else None,
        "mom5": (last / float(close.iloc[-6]) - 1) * 100 if len(close) > 6 else None,
        "mom20": (last / float(close.iloc[-21]) - 1) * 100,
        "vol_ratio": (float(vol.iloc[-1]) / float(vol.tail(20).mean())) if len(vol) >= 20 else None,
    }


def cross_section(ticker: str) -> str:
    """Peer cross-section for a non-A-share ticker, framed like the A-share one."""
    import yfinance as yf

    tick = (ticker or "").strip().upper()
    peers = US_PEERS.get(tick, ())
    if not peers:
        return (
            f"DATA_UNAVAILABLE: {tick} 不在面板覆盖范围（仅 A 股），且未收录其同业映射。"
            "请用 get_indicators 做纵向分析，横向对比改用同板块代表标的自行说明。"
        )

    symbols = [tick, *peers]
    rows: dict[str, dict] = {}
    try:
        # 一次批量下载，避免逐只请求（每只一次会变成 4-5 次往返）。
        data = yf.download(symbols, period="2mo", interval="1d",
                           group_by="ticker", progress=False, threads=True)
    except Exception as exc:  # noqa: BLE001
        return f"DATA_UNAVAILABLE: 美股同业行情拉取失败（{type(exc).__name__}: {exc}）"

    for sym in symbols:
        try:
            hist = data[sym] if len(symbols) > 1 else data
            st = _stats(hist)
            if st:
                rows[sym] = st
        except (KeyError, TypeError):
            continue

    if tick not in rows:
        return f"DATA_UNAVAILABLE: {tick} 行情为空（Yahoo 无数据或标的不可交易）"

    me = rows[tick]
    lines = [f"## 同业横截面 — {_NAME.get(tick, tick)}（{tick}）",
             "> 面板只覆盖 A 股，这里用 Yahoo 同业数据给出相对位置。",
             "",
             "| 标的 | 名称 | 收盘 | 今日 | 5日 | 20日 | 量比 |",
             "|---|---|---:|---:|---:|---:|---:|"]

    def fmt(v, suffix="%"):
        return "-" if v is None else f"{v:+.2f}{suffix}"

    for sym, st in sorted(rows.items(), key=lambda kv: kv[1]["chg"] or -99, reverse=True):
        mark = "**" if sym == tick else ""
        vol_ratio = "-" if st["vol_ratio"] is None else f"{st['vol_ratio']:.2f}"
        lines.append(
            f"| {mark}{sym}{mark} | {_NAME.get(sym, sym)} | {st['last']:.2f} | "
            f"{fmt(st['chg'])} | {fmt(st['mom5'])} | {fmt(st['mom20'])} | {vol_ratio} |"
        )

    chg_rank = sorted(rows, key=lambda s: rows[s]["chg"] or -99, reverse=True).index(tick) + 1
    mom_rank = sorted(rows, key=lambda s: rows[s]["mom20"] or -99, reverse=True).index(tick) + 1
    lines.append("")
    lines.append(f"- 同业 {len(rows)} 只中：**今日涨跌排第 {chg_rank}**，**20日动量排第 {mom_rank}**。")
    peer_chg = [rows[s]["chg"] for s in rows if s != tick and rows[s]["chg"] is not None]
    if peer_chg:
        med = sorted(peer_chg)[len(peer_chg) // 2]
        diff = (me["chg"] or 0) - med
        lines.append(f"- 同业中位数 {med:+.2f}%，本标的 {me['chg']:+.2f}%，"
                     f"{'强于' if diff >= 0 else '弱于'}中位数 **{abs(diff):.2f}pp**。")
    if me.get("vol_ratio"):
        lines.append(f"- 量比 {me['vol_ratio']:.2f}："
                     f"{'放量' if me['vol_ratio'] > 1.3 else '缩量' if me['vol_ratio'] < 0.8 else '量能平稳'}。")
    lines.append("")
    lines.append("> 同业映射由人工维护（`US_PEERS`）；跨市场标的（如 .KS 韩股）收盘时点不同，"
                 "当日涨跌仅供参考。")
    return "\n".join(lines)
