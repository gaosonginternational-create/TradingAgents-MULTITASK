"""AkShare domestic order-book (盘口) snapshot.

The finest-grained *freely* available domestic quote is the five-level order
book (买一~买五 / 卖一~卖五) from Eastmoney's ``stock_bid_ask_em``. True
millisecond Level-2 depth (逐笔委托/逐笔成交) is a paid broker feed and not
available through a free source, so this five-level snapshot is what we use to
read intraday supply/demand pressure and short-term capital posturing.

From the five levels we compute the standard market-microstructure signals:
  - 买卖价差 (spread): sell_1 - buy_1 — the tighter, the more liquid
  - 委比 (bid-ask imbalance): (总买量 - 总卖量) / (总买量 + 总卖量) — the
    signed -1..+1 ratio of resting buy vs sell volume
  - 总买量 / 总卖量: total resting bid/ask volume across five levels
  - 买卖压力: a coarse read of whether the book leans toward buyers or sellers

Note this is a *snapshot* (point-in-time), and resting orders can be cancelled
or spoofed, so the analyst should treat it as a short-term supply/demand lean,
not a definitive direction.
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
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        # Eastmoney renders a resting order with no quote as "-" (and volumes as
        # a run of dashes); treat those as "no value" so callers skip them.
        if not s or s in ("-", "--") or set(s) <= {"-"}:
            return None
    try:
        f = float(v)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


def get_orderbook(ticker: str) -> str:
    """Five-level order-book snapshot for a Chinese-market code.

    Returns a markdown block with 买一~买五/卖一~卖五 prices and volumes, the
    spread, 委比 (bid-ask imbalance), total resting bid/ask volume, and a
    supply/demand lean. Degrades to a clear unavailable signal on any failure.
    """
    import akshare as ak

    if not is_cn_market(ticker):
        raise NoMarketDataError(
            ticker, ticker, f"'{ticker}' is not a recognized A-share/HK/ETF code"
        )
    code = normalize_ak_symbol(ticker)

    lines = [f"## {code} 五档盘口 (东财):\n"]

    # Eastmoney's five-level order book (stock_bid_ask_em) covers stocks only;
    # an ETF has no per-share bid/ask board. Degrade to a clear note instead of
    # a TypeError — the ETF's money flow / fund-flow read is the right signal.
    if is_etf(ticker):
        lines.append(
            "### 盘口：ETF 无个股五档盘口数据（东财盘口仅覆盖个股）。\n"
            "参考基金主力资金流与份额变化（见情绪/资金流数据块）作为资金面信号。\n"
        )
        return "\n".join(lines).rstrip()

    try:
        book = ak_retry(lambda: ak.stock_bid_ask_em(symbol=code))
    except VendorUnavailableError as e:
        lines.append(f"### 盘口：<unavailable: {e}>\n")
        return "\n".join(lines).rstrip()

    if book is None or book.empty:
        lines.append("### 盘口：未返回数据\n")
        return "\n".join(lines).rstrip()

    # stock_bid_ask_em returns a key/value frame: item -> value.
    kv = {}
    for _, row in book.iterrows():
        item = str(row.get("item") or "").strip()
        val = _fmt_num(row.get("value"))
        kv[item] = val

    def _get(name):
        return kv.get(name)

    # Collect five levels of bids and asks
    bid_rows, ask_rows = [], []
    for i in range(1, 6):
        bp = _get(f"buy_{i}")
        bv = _get(f"buy_{i}_vol")
        ap = _get(f"sell_{i}")
        av = _get(f"sell_{i}_vol")
        if bp is not None:
            bid_rows.append((bp, bv if bv is not None else 0.0))
        if ap is not None:
            ask_rows.append((ap, av if av is not None else 0.0))

    if not bid_rows and not ask_rows:
        lines.append("### 盘口：无有效五档数据（可能停牌）\n")
        return "\n".join(lines).rstrip()

    # Render asks (卖) top-down so 卖一 sits at the bottom, then bids (买)
    lines.append("### 盘口明细（卖盘自上而下，买盘自下而上）")
    for price, vol in reversed(ask_rows):
        lines.append(f"- 卖: {price:.2f} × {vol:,.0f} 手")
    for price, vol in bid_rows:
        lines.append(f"- 买: {price:.2f} × {vol:,.0f} 手")

    best_bid = bid_rows[0][0] if bid_rows else None
    best_ask = ask_rows[0][0] if ask_rows else None
    total_bid = sum(v for _, v in bid_rows)
    total_ask = sum(v for _, v in ask_rows)

    lines.append("")
    lines.append("### 盘口指标")
    if best_bid is not None and best_ask is not None:
        spread = best_ask - best_bid
        lines.append(f"- 买一价: {best_bid:.2f}，卖一价: {best_ask:.2f}")
        lines.append(f"- 买卖价差: {spread:.3f}（{'较薄，流动性好' if spread <= 0.05 else '偏宽，流动性一般'}）")
    if total_bid + total_ask > 0:
        imbalance = (total_bid - total_ask) / (total_bid + total_ask)
        lines.append(f"- 总买量: {total_bid:,.0f} 手，总卖量: {total_ask:,.0f} 手")
        lines.append(f"- 委比: {imbalance:+.2f}（{'买盘占优' if imbalance > 0.1 else '卖盘占优' if imbalance < -0.1 else '买卖均衡'}）")
        if imbalance > 0.2:
            lean = "买方挂单明显更厚，短期承接意愿强（但需警惕大单诱多后撤单）"
        elif imbalance < -0.2:
            lean = "卖方挂单明显更厚，短期抛压偏重"
        else:
            lean = "买卖力量相对均衡，方向不明朗"
        lines.append(f"- 解读线索: {lean}")
    lines.append("")

    return "\n".join(lines).rstrip()
