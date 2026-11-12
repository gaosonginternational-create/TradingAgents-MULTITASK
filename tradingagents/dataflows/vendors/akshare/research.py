"""A-share research reports, sell-side forecasts, and downstream-customer analysis.

The fundamentals analyst needs *forward-looking* demand signals that the
company's own trailing financial statements cannot show. Two sources fill that
gap, both free and keyless:

1. **Sell-side research** — Eastmoney's per-stock research-report feed
   (``stock_research_report_em``) carries each brokerage's rating, the report
   title (which usually names the *driver*: "订单能见度增强", "Strong orders",
   "AI 资本开支上修"…), and a **multi-year EPS forecast** (2026/2027/2028
   每股收益 + 市盈率). This is the structured "analyst estimate" layer.

2. **Downstream-customer triangulation** — a company's revenue is a function of
   its *customers'* spend. For a semiconductor-equipment maker the driver is
   the wafer fabs' capex; for an optical-module maker it is the hyperscalers'
   AI capex; for a connector maker it is a phone/EV maker's build plan. By
   pulling the *downstream customers'* own research reports and profit
   forecasts, the analyst can project the target company's orders and revenue
   from the demand side instead of extrapolating only its trailing numbers.

``get_research_reports`` returns the target's own sell-side forecasts;
``get_downstream_analysis`` resolves the target's downstream customers (via a
curated map, falling back to analyst inference) and aggregates their research
so the LLM can triangulate demand.
"""

from __future__ import annotations

import logging

import pandas as pd

from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.vendors.akshare.common import (
    ak_retry,
    is_a_share,
    normalize_ak_symbol,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Downstream-customer map: company code -> list of (customer_code_or_ticker,
# customer_name, relationship note).  Customers that are themselves listed
# A-share companies (6-digit codes) get their research pulled automatically.
# Overseas / unlisted customers are listed by name only so the LLM can combine
# them with its own knowledge (their capex/guidance is not in AkShare, but the
# analyst knows e.g. Nvidia/Apple/Samsung guidance from training data + news).
# ---------------------------------------------------------------------------
DOWNSTREAM_MAP: dict[str, list[tuple[str, str, str]]] = {
    # 半导体设备 → 晶圆厂 (capex 驱动设备订单)
    "002371": [  # 北方华创（刻蚀/薄膜/清洗/炉管设备）
        ("688981", "中芯国际", "晶圆代工龙头，扩产资本开支直接驱动刻蚀/薄膜/清洗设备订单"),
        ("688249", "晶合集成", "晶圆代工，扩产驱动设备订单"),
        ("688347", "华虹公司", "晶圆代工，产能爬坡与扩产驱动设备需求"),
    ],
    "688012": [  # 中微公司（刻蚀设备）
        ("688981", "中芯国际", "晶圆代工扩产带动刻蚀设备订单"),
        ("688347", "华虹公司", "晶圆代工扩产驱动刻蚀设备需求"),
    ],
    # 光模块 → 云厂商/AI 算力 (AI capex 驱动)
    "300308": [  # 中际旭创
        ("NVDA", "英伟达", "海外：AI GPU 出货与数据中心 capex 直接决定 800G/1.6T 光模块需求"),
        ("MSFT", "微软", "海外：Azure AI capex 上修→光模块订单"),
        ("0700.HK", "腾讯", "国内：智算中心 capex 驱动国内光模块需求"),
    ],
    # 连接器/消费电子 → 终端品牌 + AI 服务器
    "002475": [  # 立讯精密
        ("AAPL", "苹果", "消费电子：iPhone/AirPods 出货与新品周期决定连接器/声学/组装订单"),
        ("NVDA", "英伟达", "AI 服务器：GB 系列机柜连接器/铜缆/电源订单"),
    ],
    # 内存接口芯片 → 存储原厂 (DRAM/DDR5 迭代驱动)
    "688008": [  # 澜起科技
        ("Samsung", "三星电子", "海外：DRAM 原厂 DDR5 渗透与扩产驱动内存接口/配套芯片需求"),
        ("SKHY", "SK海力士", "海外：HBM/DDR5 扩产驱动配套芯片需求"),
        ("MU", "美光科技", "海外：存储涨价周期与扩产驱动接口芯片需求"),
    ],
    # 铜合金/新材料 → 电力设备/轨交 (电网/轨交投资驱动)
    "688102": [  # 斯瑞新材
        ("601766", "中国中车", "轨交牵引/接触材料需求"),
        ("600089", "特变电工", "电网/输变电设备用铜合金材料需求"),
    ],
}

# Report-title keywords that flag a *demand/order/capex* driver — the signals
# the analyst should read as "downstream demand is moving".
_DEMAND_KEYWORDS = (
    "订单", "能见度", "供需", "扩产", "capex", "资本开支", "需求", "景气",
    "涨价", "提价", "供不应求", "紧缺", "AI", "算力", "数据中心",
    "order", "demand", "supply", "ramp", "gross margin", "guidance",
)


def get_research_reports(ticker: str, limit: int = 15) -> str:
    """Sell-side research reports + multi-year EPS forecasts for an A-share.

    Returns a bounded markdown block: each report's rating, brokerage, date,
    title, and the 2026/2027/2028 EPS + PE forecast, with demand/capex/order
    signals flagged. This is the structured analyst-estimate layer the
    fundamentals analyst uses to project future earnings.
    """
    if not is_a_share(ticker):
        raise NoMarketDataError(
            ticker, ticker, f"'{ticker}' is not a recognized A-share code"
        )
    code = normalize_ak_symbol(ticker)

    import akshare as ak

    df = ak_retry(lambda: ak.stock_research_report_em(symbol=code))
    if df is None or df.empty:
        raise VendorUnavailableError(f"No research reports for {code}")

    name = str(df.iloc[0].get("股票简称") or "").strip() or code
    lines = [f"# 卖方研报与盈利预测 Research for {name} ({code})", ""]

    # Latest ratings summary: how many 买入/增持/中性 etc.
    if "东财评级" in df.columns:
        rating_counts = df["东财评级"].value_counts()
        summary = "、".join(f"{k}{v}" for k, v in rating_counts.items())
        lines.append(f"- 评级分布（近 {len(df)} 篇）: {summary}")
        lines.append("")

    for _, row in df.head(limit).iterrows():
        title = str(row.get("报告名称") or "").strip()
        rating = str(row.get("东财评级") or "").strip()
        org = str(row.get("机构") or "").strip()
        when = str(row.get("日期") or "").strip()
        if not title:
            continue

        # Flag demand/capex/order drivers in the title.
        flag = "【需求/订单信号】" if any(k in title for k in _DEMAND_KEYWORDS) else ""
        lines.append(f"### {title} {flag}")
        if org:
            lines.append(f"- 机构: {org}" + (f" | 评级: {rating}" if rating else ""))
        if when:
            lines.append(f"- 日期: {when}")

        # Multi-year EPS + PE forecast (2026/2027/2028).
        fwd = []
        for yr in ("2026", "2027", "2028"):
            eps = row.get(f"{yr}-盈利预测-收益")
            pe = row.get(f"{yr}-盈利预测-市盈率")
            if eps is not None and not pd.isna(eps):
                seg = f"{yr}E EPS {float(eps):.2f} 元"
                if pe is not None and not pd.isna(pe):
                    seg += f" / PE {float(pe):.1f} 倍"
                fwd.append(seg)
        if fwd:
            lines.append("- 盈利预测: " + " | ".join(fwd))
        lines.append("")

    return "\n".join(lines).rstrip()


def get_downstream_analysis(ticker: str, limit: int = 6) -> str:
    """Downstream-customer demand triangulation for an A-share ``ticker``.

    Resolves the target's downstream customers (curated map, with analyst
    inference fallback) and aggregates *their* research reports / profit
    forecasts, so the analyst can project the target's orders and revenue from
    the demand side (customer capex, build plans, guidance) rather than only
    the target's trailing numbers.
    """
    if not is_a_share(ticker):
        raise NoMarketDataError(
            ticker, ticker, f"'{ticker}' is not a recognized A-share code"
        )
    code = normalize_ak_symbol(ticker)

    import akshare as ak

    customers = DOWNSTREAM_MAP.get(code, [])
    lines = [f"# 下游客户需求三角推导 Downstream Demand for {code}", ""]

    if not customers:
        lines.append(
            "- 未配置下游客户映射。请结合公司所处环节（设备/材料/组件/整机），"
            "由分析师推断其主要下游客户，并抓取下游客户的研报/资本开支/业绩前瞻来推导目标公司订单与营收。"
        )
        return "\n".join(lines)

    lines.append("- 下游客户及其驱动逻辑：")
    for ccode, cname, note in customers:
        lines.append(f"  - {cname} ({ccode}): {note}")
    lines.append("")

    # Pull research for each *listed A-share* downstream customer (6-digit code).
    listed = [(c, n, d) for c, n, d in customers if c.isdigit() and len(c) == 6]
    overseas = [(c, n, d) for c, n, d in customers if not (c.isdigit() and len(c) == 6)]

    if listed:
        lines.append("## 下游客户（A股上市）研报与盈利预测")
        for ccode, cname, note in listed:
            try:
                df = ak_retry(lambda: ak.stock_research_report_em(symbol=ccode))
            except Exception as e:  # noqa: BLE001
                logger.warning("downstream research failed for %s: %s", ccode, e)
                lines.append(f"\n### {cname} ({ccode})\n- 研报数据不可用")
                continue
            if df is None or df.empty:
                lines.append(f"\n### {cname} ({ccode})\n- 无研报覆盖")
                continue
            lines.append(f"\n### {cname} ({ccode})")
            lines.append(f"- 驱动: {note}")
            for _, row in df.head(limit).iterrows():
                title = str(row.get("报告名称") or "").strip()
                rating = str(row.get("东财评级") or "").strip()
                org = str(row.get("机构") or "").strip()
                when = str(row.get("日期") or "").strip()
                if not title:
                    continue
                fwd = []
                for yr in ("2026", "2027", "2028"):
                    eps = row.get(f"{yr}-盈利预测-收益")
                    pe = row.get(f"{yr}-盈利预测-市盈率")
                    if eps is not None and not pd.isna(eps):
                        seg = f"{yr}E EPS {float(eps):.2f}"
                        if pe is not None and not pd.isna(pe):
                            seg += f"/PE{float(pe):.0f}"
                        fwd.append(seg)
                head = f"  - {title}"
                if rating:
                    head += f" [{rating}]"
                if org:
                    head += f" ({org})"
                lines.append(head)
                if fwd:
                    lines.append(f"    盈利预测: {' | '.join(fwd)}")
                if when:
                    lines.append(f"    日期: {when}")
        lines.append("")

    if overseas:
        lines.append("## 下游客户（海外/非上市）")
        for ccode, cname, note in overseas:
            lines.append(
                f"- {cname} ({ccode}): {note}。此类客户财报/资本开支不在 AkShare 覆盖内，"
                "请结合你的行业知识与其最新业绩指引/capex（如 AI 算力资本开支、终端出货指引）来推导目标公司订单。"
            )

    lines.append("")
    lines.append(
        "## 推导方法提示\n"
        "用下游客户的资本开支/出货/业绩前瞻指引，自下而上估算目标公司的订单与营收：\n"
        "1. 找到驱动变量（客户 capex、扩产产能、出货量、资本开支）及其增速；\n"
        "2. 估算目标公司在该客户的份额与单机/单量价值量，换算成营收；\n"
        "3. 叠加公司毛利率/费用率，得到前瞻净利润；\n"
        "4. 用行业适用的估值倍数（PEG/PS/前瞻PE/EV-EBITDA）对比当前市值，判断是否低估。"
    )
    return "\n".join(lines)
