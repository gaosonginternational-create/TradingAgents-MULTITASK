"""A-share financial statements (income / balance sheet / cash flow).

AkShare exposes the three A-share financial statements through the Eastmoney
"by report period" endpoints (``stock_profit_sheet_by_report_em``,
``stock_balance_sheet_by_report_em``, ``stock_cash_flow_sheet_by_report_em``).
Each returns a wide table (100+ columns) of every reporting line across the
company's history, oldest-first.

This module holds the three ``get_*`` entry points and the shared rendering
helpers. The public functions mirror the Yahoo/Alpha Vantage signatures
(``ticker``, ``freq``, ``as_of_date``) so they drop into the router's vendor
chain unchanged, and each returns a bounded Markdown + CSV block that a
fundamentals analyst can consume: a headline summary of the latest period, a
recent-period trend, and the raw table capped to the latest 8 periods.
"""

from __future__ import annotations

import logging

import pandas as pd

from tradingagents.dataflows.date_window import is_historical
from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.vendors.akshare.common import (
    ak_retry,
    is_a_share,
    normalize_ak_symbol,
    raise_for_empty,
)

logger = logging.getLogger(__name__)

# How many recent periods to keep in the raw CSV (oldest-first). A-share
# statements are cumulative within a fiscal year, so 8 periods spans ~2 years
# and shows the trend without blowing up the prompt.
_MAX_PERIODS = 8

# Eastmoney exchange prefix a bare 6-digit code needs for the *_by_report_em
# endpoints (they require SH600519 / SZ000651 style symbols).
def _exchange_prefixed(code: str) -> str:
    if code.startswith(("60", "68")):
        return f"SH{code}"
    if code.startswith(("00", "30")):
        return f"SZ{code}"
    return code  # Beijing (4xx/8xx): pass through; endpoint coverage is spotty.


def _fetch_statement(code: str, kind: str):
    """Return the Eastmoney DataFrame for ``kind`` (profit/balance/cashflow)."""
    import akshare as ak

    fetchers = {
        "profit": lambda ak, sym: ak.stock_profit_sheet_by_report_em(symbol=sym),
        "balance": lambda ak, sym: ak.stock_balance_sheet_by_report_em(symbol=sym),
        "cashflow": lambda ak, sym: ak.stock_cash_flow_sheet_by_report_em(symbol=sym),
    }
    return ak_retry(lambda: fetchers[kind](ak, _exchange_prefixed(code)))


def _render_statement(code: str, ticker: str, kind: str, df: pd.DataFrame) -> str:
    """Render a statement as a headline summary + recent trend + bounded CSV."""
    # Eastmoney tables are newest-first (row 0 = latest report period).
    latest = df.iloc[0]

    def num(*cols):
        for c in cols:
            if c in df.columns:
                v = latest.get(c)
                if v is not None and not pd.isna(v):
                    return float(v)
        return None

    def yi(v):
        return None if v is None else f"{v / 1e8:.2f} 亿元"

    def pct(*cols):
        v = num(*cols)
        return None if v is None else f"{v:.2f}%"

    # Report period / type for the header.
    rpt = latest.get("REPORT_DATE")
    rpt_name = latest.get("REPORT_DATE_NAME")
    period_label = f"{rpt_name}（{pd.to_datetime(rpt).strftime('%Y-%m-%d')}）" if rpt is not None else "最新报告期"

    # Notice date (announcement / filing date). Eastmoney carries it as
    # NOTICE_DATE; showing it lets the analyst see *when* the figures became
    # public, which is what makes a point-in-time run auditable.
    notice = latest.get("NOTICE_DATE")
    notice_label = ""
    if notice is not None and not pd.isna(notice):
        try:
            notice_label = f"，公告日 {pd.to_datetime(notice).strftime('%Y-%m-%d')}"
        except (ValueError, TypeError):
            notice_label = ""

    if kind == "profit":
        revenue = num("OPERATE_INCOME", "TOTAL_OPERATE_INCOME")
        cost = num("OPERATE_COST", "TOTAL_OPERATE_COST")
        gross = None if (revenue is None or cost is None) else revenue - cost
        fields = [
            ("营业收入", yi(revenue)),
            ("营业成本", yi(cost)),
            ("毛利润", yi(gross)),
            ("销售费用", yi(num("SALE_EXPENSE"))),
            ("管理费用", yi(num("MANAGE_EXPENSE"))),
            ("研发费用", yi(num("RESEARCH_EXPENSE", "ME_RESEARCH_EXPENSE"))),
            ("财务费用", yi(num("FINANCE_EXPENSE"))),
            ("营业利润", yi(num("OPERATE_PROFIT"))),
            ("利润总额", yi(num("TOTAL_PROFIT"))),
            ("净利润", yi(num("NETPROFIT"))),
            ("归母净利润", yi(num("PARENT_NETPROFIT"))),
            ("扣非净利润", yi(num("DEDUCT_PARENT_NETPROFIT"))),
            ("营收同比", pct("OPERATE_INCOME_YOY", "TOTAL_OPERATE_INCOME_YOY")),
            ("净利润同比", pct("PARENT_NETPROFIT_YOY", "NETPROFIT_YOY")),
        ]
        title = "利润表 Income Statement"
    elif kind == "balance":
        total_assets = num("TOTAL_ASSETS")
        total_liab = num("TOTAL_LIABILITIES")
        debt_ratio = None if (total_assets is None or total_liab is None) else total_liab / total_assets * 100
        fields = [
            ("资产总计", yi(total_assets)),
            ("流动资产", yi(num("TOTAL_CURRENT_ASSETS"))),
            ("货币资金", yi(num("MONETARYFUNDS", "MONETARY_FUNDS"))),
            ("应收账款", yi(num("ACCOUNTS_RECE", "ACCOUNTS_RECEIVABLE"))),
            ("存货", yi(num("INVENTORY"))),
            ("固定资产", yi(num("FIXED_ASSET", "FIXED_ASSETS"))),
            ("负债合计", yi(total_liab)),
            ("流动负债", yi(num("TOTAL_CURRENT_LIAB"))),
            ("股东权益合计", yi(num("TOTAL_EQUITY", "TOTAL_PARENT_EQUITY"))),
            ("归母股东权益", yi(num("TOTAL_PARENT_EQUITY"))),
            ("资产负债率", None if debt_ratio is None else f"{debt_ratio:.2f}%"),
        ]
        title = "资产负债表 Balance Sheet"
    else:  # cashflow
        fields = [
            ("经营活动现金流净额", yi(num("NETCASH_OPERATE", "NETCASH_OPERATE_ACT"))),
            ("投资活动现金流净额", yi(num("NETCASH_INVEST", "NETCASH_INVEST_ACT"))),
            ("筹资活动现金流净额", yi(num("NETCASH_FINANCE", "NETCASH_FINANCE_ACT"))),
            ("销售商品收到现金", yi(num("SALES_SERVICES", "RECEIVE_ORIGINAL_INSURANCE_FEE"))),
            ("购建资产支付现金", yi(num("CONSTRUCT_LONG_ASSET"))),
            ("期末现金及等价物", yi(num("CCE_ADD", "END_CCE"))),
            ("现金及等价物净增加", yi(num("CCE_ADD"))),
            ("自由现金流(经营-购建)", yi(_fcf(num("NETCASH_OPERATE", "NETCASH_OPERATE_ACT"), num("CONSTRUCT_LONG_ASSET")))),
        ]
        title = "现金流量表 Cash Flow"

    lines = [f"{label}: {v}" for label, v in fields if v is not None and v != ""]
    if not lines:
        raise NoMarketDataError(ticker, code, f"no {kind} statement fields")

    header = f"# {title} for {code}（来自 {ticker}）\n\n"
    headline = f"最新报告期：{period_label}{notice_label}\n\n" + "\n".join(lines)

    # Recent-period CSV, newest-first as Eastmoney returns it.
    csv = df.head(_MAX_PERIODS).to_csv(index=False)
    return f"{header}{headline}\n\n## 最近 {min(_MAX_PERIODS, len(df))} 期明细（新→旧）\n\n{csv}"


def _fcf(ocf, capex):
    if ocf is None or capex is None:
        return None
    return ocf - capex


# --- Sina statement fallback ---------------------------------------------
# Eastmoney's *datacenter* host (not push2) serves the primary statements.
# If it ever fails, Sina serves the same statements from a different host with
# numerically identical figures (verified: 603986 2026三季报 营收/净利/归母
# match to the cent). Sina carries only the report date (报告日), no filing
# date, so it is used only for a today-dated run — a historical run keeps the
# honest unavailable signal rather than risk look-ahead.
_SINA_LABELS = {"profit": "利润表", "balance": "资产负债表", "cashflow": "现金流量表"}


def _sina_symbol(code: str) -> str:
    """``603986`` -> ``sh603986``; ``000001`` -> ``sz000001``."""
    if code.startswith(("60", "68", "9")):
        return f"sh{code}"
    return f"sz{code}"


def _fetch_sina_statement(code: str, kind: str):
    """Sina structured statement (a different host from Eastmoney datacenter)."""
    import akshare as ak

    return ak_retry(lambda: ak.stock_financial_report_sina(
        stock=_sina_symbol(code), symbol=_SINA_LABELS[kind]))


def _render_sina_statement(code: str, ticker: str, kind: str, df) -> str:
    """Render a Sina statement frame (Chinese columns) like the Eastmoney one."""
    latest = df.iloc[0]

    def num(*cols):
        for c in cols:
            if c in df.columns:
                v = latest.get(c)
                if v is not None and not pd.isna(v):
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        return None
        return None

    def yi(v):
        return None if v is None else f"{v / 1e8:.2f} 亿元"

    rpt = str(latest.get("报告日") or "").strip()
    period_label = f"{rpt[:4]}-{rpt[4:6]}-{rpt[6:8]}" if len(rpt) == 8 else (rpt or "最新报告期")

    if kind == "profit":
        revenue = num("营业总收入", "营业收入")
        cost = num("营业成本")
        gross = None if (revenue is None or cost is None) else revenue - cost
        fields = [
            ("营业收入", yi(revenue)),
            ("营业成本", yi(cost)),
            ("毛利润", yi(gross)),
            ("销售费用", yi(num("销售费用"))),
            ("管理费用", yi(num("管理费用"))),
            ("研发费用", yi(num("研发费用"))),
            ("财务费用", yi(num("财务费用"))),
            ("营业利润", yi(num("营业利润"))),
            ("利润总额", yi(num("利润总额"))),
            ("净利润", yi(num("净利润"))),
            ("归母净利润", yi(num("归属于母公司所有者的净利润"))),
            ("扣非净利润", yi(num("扣除非经常性损益后的净利润"))),
        ]
        title = "利润表 Income Statement"
        csv_cols = ["报告日", "营业总收入", "营业成本", "营业利润", "利润总额",
                    "净利润", "归属于母公司所有者的净利润"]
    elif kind == "balance":
        total_assets = num("资产总计")
        total_liab = num("负债合计")
        debt_ratio = None if (total_assets is None or total_liab is None) else total_liab / total_assets * 100
        fields = [
            ("资产总计", yi(total_assets)),
            ("流动资产", yi(num("流动资产合计"))),
            ("货币资金", yi(num("货币资金"))),
            ("应收账款", yi(num("应收账款", "应收票据及应收账款"))),
            ("存货", yi(num("存货"))),
            ("固定资产", yi(num("固定资产净额", "固定资产"))),
            ("负债合计", yi(total_liab)),
            ("流动负债", yi(num("流动负债合计"))),
            ("股东权益合计", yi(num("所有者权益(或股东权益)合计", "所有者权益"))),
            ("资产负债率", None if debt_ratio is None else f"{debt_ratio:.2f}%"),
        ]
        title = "资产负债表 Balance Sheet"
        csv_cols = ["报告日", "资产总计", "货币资金", "应收账款", "存货",
                    "负债合计", "所有者权益(或股东权益)合计"]
    else:  # cashflow
        fields = [
            ("经营活动现金流净额", yi(num("经营活动产生的现金流量净额"))),
            ("投资活动现金流净额", yi(num("投资活动产生的现金流量净额"))),
            ("筹资活动现金流净额", yi(num("筹资活动产生的现金流量净额"))),
            ("购建资产支付现金", yi(num("购建固定资产、无形资产和其他长期资产所支付的现金"))),
            ("期末现金及等价物", yi(num("期末现金及现金等价物余额"))),
        ]
        title = "现金流量表 Cash Flow"
        csv_cols = ["报告日", "经营活动产生的现金流量净额",
                    "投资活动产生的现金流量净额", "筹资活动产生的现金流量净额",
                    "购建固定资产、无形资产和其他长期资产所支付的现金"]

    lines = [f"{label}: {v}" for label, v in fields if v is not None and v != ""]
    if not lines:
        raise NoMarketDataError(ticker, code, f"no {kind} statement fields (Sina)")

    header = f"# {title} for {code}（来自 {ticker}，备用源：新浪）\n\n"
    headline = f"最新报告期：{period_label}\n\n" + "\n".join(lines)

    csv_cols = [c for c in csv_cols if c in df.columns]
    csv = df.head(_MAX_PERIODS)[csv_cols].to_csv(index=False)
    return f"{header}{headline}\n\n## 最近 {min(_MAX_PERIODS, len(df))} 期明细（新→旧）\n\n{csv}"


def _filter_as_of(df: pd.DataFrame, as_of_date: str | None) -> pd.DataFrame:
    """Keep only statement rows whose announcement date is on/before ``as_of_date``.

    Eastmoney's statement tables carry ``NOTICE_DATE`` (the day the report was
    announced / filed). Filtering on it makes an A-share run genuinely
    point-in-time: a run dated 2026-11-12 sees the 2026 三季报 announced
    2026-10-30, but a run dated 2026-10-01 does not — even though the report
    period (2026-09-30) had already ended. Without this filter the analyst could
    read figures that were not yet public on the analysis date (look-ahead bias);
    withholding everything instead throws away data that *was* public. This is
    the as-filed middle ground that SEC EDGAR gives US filers.

    Returns ``df`` unchanged when there is no date to filter on or no
    ``NOTICE_DATE`` column, and an empty frame when nothing was public yet.
    """
    if not as_of_date or "NOTICE_DATE" not in df.columns:
        return df
    notice = pd.to_datetime(df["NOTICE_DATE"], errors="coerce")
    target = pd.to_datetime(as_of_date, errors="coerce")
    if pd.isna(target):
        return df
    mask = notice.notna() & (notice <= target)
    return df[mask]


def _statement_entry(ticker: str, freq: str, as_of_date: str | None, kind: str, title: str) -> str:
    """Shared entry point for the three ``get_*`` functions."""
    if not is_a_share(ticker):
        raise NoMarketDataError(
            ticker, ticker, f"'{ticker}' is not a recognized A-share code"
        )
    code = normalize_ak_symbol(ticker)

    em_error = None
    data = None
    source = "eastmoney"
    try:
        data = _fetch_statement(code, kind)
    except Exception as exc:  # noqa: BLE001 - fall back to Sina below
        em_error = exc
        logger.warning("Eastmoney %s unavailable for %s: %s", kind, code, exc)

    # Sina fallback only for a today-dated run: Sina has no filing date, so a
    # historical run cannot prove these figures were public on that date.
    if (data is None or data.empty) and not is_historical(as_of_date):
        try:
            data = _fetch_sina_statement(code, kind)
            source = "sina"
            logger.warning("Using Sina %s fallback for %s", kind, code)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Sina %s fallback also failed for %s: %s", kind, code, exc)

    if data is None or data.empty:
        if em_error is not None:
            raise em_error
        raise_for_empty(ticker, code, f"{title} data")

    if source == "eastmoney":
        # Point-in-time: drop reports not yet announced on the analysis date.
        if as_of_date:
            filtered = _filter_as_of(data, as_of_date)
            if filtered is None or filtered.empty:
                raise NoMarketDataError(
                    ticker, code,
                    f"no {title} was announced on or before {as_of_date} "
                    f"(the vendor reports NOTICE_DATE, so this is a real gap, not a fetch failure)",
                )
            data = filtered
        return _render_statement(code, ticker, kind, data)

    return _render_sina_statement(code, ticker, kind, data)


def get_income_statement(
    ticker: str,
    freq: str = "quarterly",
    as_of_date: str | None = None,
) -> str:
    """A-share income statement from Eastmoney (per report period).

    ``freq`` is accepted for signature parity with the Yahoo/Alpha Vantage
    counterparts; Eastmoney's per-report-period table covers both annual and
    interim periods, so it is returned as-is regardless of ``freq``.
    """
    return _statement_entry(ticker, freq, as_of_date, "profit", "income statement")


def get_balance_sheet(
    ticker: str,
    freq: str = "quarterly",
    as_of_date: str | None = None,
) -> str:
    """A-share balance sheet from Eastmoney (per report period)."""
    return _statement_entry(ticker, freq, as_of_date, "balance", "balance sheet")


def get_cashflow(
    ticker: str,
    freq: str = "quarterly",
    as_of_date: str | None = None,
) -> str:
    """A-share cash flow statement from Eastmoney (per report period)."""
    return _statement_entry(ticker, freq, as_of_date, "cashflow", "cash flow")
