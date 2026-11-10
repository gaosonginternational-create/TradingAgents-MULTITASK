"""AkShare China macro vendor.

The FRED vendor serves US (and euro-area) macro series, but a company whose
revenue is overwhelmingly domestic (a 贵州茅台, a 格力电器, or a domestic-facing
ETF) is driven by *China's* macro cycle — PMI, CPI/PPI, GDP, M2, industrial
production, retail sales, trade balance, LPR — not by the Fed. This module
mirrors ``fred.py::get_macro_data`` so it can drop into the news analyst's
macro tooling unchanged: it takes a friendly alias (or a raw AkShare function
suffix), an as-of date, and a look-back window, and returns a formatted
markdown block with the latest value and a recent-observation table.

The data comes from AkShare's ``macro_china_*`` functions, which aggregate the
NBS (国家统计局) / PBoC / customs releases. They are free and keyless. Each
function returns a full history (oldest-first or newest-first depending on the
source), so we sort by the parsed date and take the trailing window.

Note on point-in-time: unlike FRED, these feeds are *current vintage only* —
there is no historical-vintage archive. That is fine for live analysis but a
historical backtest should treat the macro read as "latest available", not
"what was known on the trade date" (see fred.py for the FRED counterpart that
does pin vintages).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from tradingagents.dataflows.vendors.akshare.common import ak_retry

logger = logging.getLogger(__name__)

# Friendly alias -> (akshare function name, target column selector, human title).
# The column selector picks the "headline" column: the level/index for PMI/CPI,
# the YoY growth for industrial production, etc. A value may be a string (one
# column), a list of candidate columns tried in order, or None (use the first
# numeric column after the date column).
MACRO_ALIASES = {
    # 景气 / 增长
    "pmi": ("macro_china_pmi", "制造业-指数", "制造业 PMI"),
    "manufacturing_pmi": ("macro_china_pmi", "制造业-指数", "制造业 PMI"),
    "non_man_pmi": ("macro_china_non_man_pmi", None, "非制造业 PMI"),
    "services_pmi": ("macro_china_cx_services_pmi_yearly", None, "财新服务业 PMI"),
    "cx_pmi": ("macro_china_cx_pmi_yearly", None, "财新制造业 PMI"),
    "gdp": ("macro_china_gdp", "国内生产总值-同比增长", "GDP 同比"),
    "gdp_abs": ("macro_china_gdp", "国内生产总值-绝对值", "GDP 绝对值(亿元)"),
    "industrial_production": ("macro_china_industrial_production_yoy", "今值", "工业增加值同比"),
    "industrial_production_yoy": ("macro_china_industrial_production_yoy", "今值", "工业增加值同比"),
    "retail_sales": ("macro_china_consumer_goods_retail", "同比增长", "社会消费品零售总额同比"),
    "social_retail": ("macro_china_consumer_goods_retail", "同比增长", "社会消费品零售总额同比"),
    # 价格
    "cpi": ("macro_china_cpi", "全国-同比增长", "CPI 同比"),
    "cpi_yoy": ("macro_china_cpi", "全国-同比增长", "CPI 同比"),
    "ppi": ("macro_china_ppi", "当月同比增长", "PPI 同比"),
    # 货币 / 金融
    "m2": ("macro_china_money_supply", "货币和准货币(M2)-同比增长", "M2 同比"),
    "money_supply": ("macro_china_money_supply", "货币和准货币(M2)-同比增长", "M2 同比"),
    "lpr": ("macro_china_lpr", None, "LPR 贷款市场报价利率"),
    "shibor": ("macro_china_shibor_all", None, "Shibor 利率"),
    "rrr": ("macro_china_reserve_requirement_ratio", None, "存款准备金率"),
    # 外贸
    "trade_balance": ("macro_china_trade_balance", "今值", "贸易差额"),
    "exports_yoy": ("macro_china_exports_yoy", "今值", "出口同比"),
    "imports_yoy": ("macro_china_imports_yoy", "今值", "进口同比"),
    # 就业
    "unemployment": ("macro_china_urban_unemployment", None, "城镇调查失业率"),
    # 景气指数（国房景气/企业景气等）
    "enterprise_boom": ("macro_china_enterprise_boom_index", None, "企业景气指数"),
}

# How many recent observations to show in the table (bounded prompt size).
MAX_ROWS = 40

# Default trailing window in days.
DEFAULT_LOOKBACK_DAYS = 365


def _parse_cn_month(s: str) -> datetime | None:
    """Parse a Chinese month string like '2024年12月份' -> datetime."""
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月", str(s))
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), 1)
    return None


def _parse_cn_quarter(s: str) -> datetime | None:
    """Parse a Chinese quarter string like '2024年第1-2季度' -> datetime."""
    m = re.search(r"(\d{4})\s*年第?\s*(\d{1,2})", str(s))
    if m:
        return datetime(int(m.group(1)), int(m.group(2)) * 3, 1)
    return None


def _parse_cn_date(s: str) -> datetime | None:
    """Parse any of the CN date formats into a sortable datetime."""
    v = str(s).strip()
    if not v or v.lower() in ("nan", "none"):
        return None
    # '2024年12月份'
    d = _parse_cn_month(v)
    if d:
        return d
    # '2024年第1季度' / '2024年第1-2季度'
    d = _parse_cn_quarter(v)
    if d:
        return d
    # '2024-12' / '2024/12'
    m = re.match(r"(\d{4})[-/](\d{1,2})", v)
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), 1)
    # '2024-12-01'
    try:
        return datetime.strptime(v, "%Y-%m-%d")
    except ValueError:
        pass
    # '2024' (yearly)
    if re.fullmatch(r"\d{4}", v):
        return datetime(int(v), 12, 31)
    return None


def _fmt_num(v):
    if v is None:
        return None
    try:
        f = float(v)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


def _resolve_alias(indicator: str) -> tuple[str, str | None, str]:
    """Resolve a friendly alias; raise ValueError with guidance on failure."""
    key = indicator.strip().lower().replace(" ", "_").replace("-", "_")
    if key in MACRO_ALIASES:
        return MACRO_ALIASES[key]
    # Allow a raw akshare function name (e.g. 'macro_china_pmi') for power users.
    import akshare as ak

    if key.startswith("macro_china_") and hasattr(ak, key):
        return (key, None, key.replace("macro_china_", "").replace("_", " ").title())
    raise ValueError(
        f"'{indicator}' is not a known China macro alias. Use one of: "
        + ", ".join(sorted(MACRO_ALIASES.keys()))
    )


def _pick_value_col(df, col_selector: str | None, date_col: str) -> str | None:
    """Choose the headline value column from ``df``."""
    if col_selector:
        candidates = [col_selector] if isinstance(col_selector, str) else list(col_selector)
        for c in candidates:
            if c in df.columns:
                return c
        return None
    # No selector: first numeric-looking column that isn't the date column.
    for c in df.columns:
        if c == date_col:
            continue
        if df[c].apply(_fmt_num).notna().any():
            return c
    return None


def get_cn_macro_data(
    indicator: str,
    as_of_date: str,
    look_back_days: int | None = None,
) -> str:
    """Fetch a China macroeconomic series as a formatted markdown report.

    Args:
        indicator: A friendly alias (e.g. 'pmi', 'cpi', 'gdp', 'm2',
            'industrial_production', 'retail_sales', 'trade_balance', 'lpr').
        as_of_date: The as-of date (yyyy-mm-dd); bounds the observation window.
        look_back_days: Trailing window; ``None`` uses DEFAULT_LOOKBACK_DAYS.

    Returns:
        A markdown report with the series title, latest value, change over the
        window, and a recent-observation table — styled like ``fred.py`` so the
        news analyst reads them the same way.
    """
    import akshare as ak

    if look_back_days is None:
        look_back_days = DEFAULT_LOOKBACK_DAYS

    try:
        fn_name, col_selector, title = _resolve_alias(indicator)
    except ValueError as e:
        return f"China macro: {e}"

    end_dt = datetime.strptime(as_of_date, "%Y-%m-%d")
    start_dt = end_dt - timedelta(days=look_back_days)

    try:
        fn = getattr(ak, fn_name)
        df = ak_retry(fn)
    except Exception as e:  # noqa: BLE001
        return f"China macro '{indicator}' unavailable: {type(e).__name__} ({e})"

    if df is None or getattr(df, "empty", True):
        return f"China macro '{indicator}': no data returned."

    df = df.copy()
    # Find the date column.
    date_col = next((c for c in df.columns if any(k in str(c) for k in ("月份", "季度", "日期", "时间"))), None)
    if date_col is None:
        return f"China macro '{indicator}': could not locate a date column (cols: {list(df.columns)})."

    df["_date"] = df[date_col].apply(_parse_cn_date)
    df = df[df["_date"].notna()].sort_values("_date")
    if df.empty:
        return f"China macro '{indicator}': no parseable dates."

    # Filter to the window, but if the source lags the window (its latest
    # observation predates start_dt), fall back to the most recent rows instead
    # of returning "no observations" — a stale-but-real series is more useful
    # than a blank, and we flag the lag explicitly.
    in_window = df[df["_date"] >= start_dt]
    lagged = in_window.empty
    if lagged:
        df = df.tail(MAX_ROWS)
    else:
        df = in_window

    value_col = _pick_value_col(df, col_selector, date_col)
    if value_col is None:
        return f"China macro '{indicator}': no numeric value column found."

    # Keep only date + value columns for the table.
    table_df = df[[date_col, value_col]].copy()
    table_df["_date"] = df["_date"]

    points = []
    for _, row in table_df.iterrows():
        v = _fmt_num(row.get(value_col))
        if v is None:
            continue
        points.append((row["_date"], str(row.get(date_col)).strip(), v))

    if not points:
        return (
            f"## 中国宏观: {title}\n"
            f"- Window: {start_dt.strftime('%Y-%m-%d')} to {as_of_date}\n"
            f"\nNo observations in this window."
        )

    first_dt, first_label, first_val = points[0]
    last_dt, last_label, last_val = points[-1]
    try:
        delta = last_val - first_val
        base = first_val
        pct = f" ({delta / base * 100:+.2f}%)" if base != 0 else ""
        summary = (
            f"\n**最新值:** {last_val:g} ({last_label}) | "
            f"**窗口内变化:** {delta:+.2f}{pct}，从 {first_val:g}\n"
        )
    except (TypeError, ValueError):
        summary = f"\n**最新值:** {last_val} ({last_label})\n"

    shown = points
    note = ""
    if len(points) > MAX_ROWS:
        shown = points[-MAX_ROWS:]
        note = f"\n_(显示最近 {MAX_ROWS} 条，共 {len(points)} 条)_\n"

    table = (
        "\n| 日期 | 数值 |\n| --- | --- |\n"
        + "\n".join(f"| {label} | {v:g} |" for _, label, v in shown)
        + "\n"
    )

    header = (
        f"## 中国宏观: {title}\n"
        f"- 数据来源: 国家统计局/央行/海关 (AkShare)\n"
        f"- Window: {start_dt.strftime('%Y-%m-%d')} to {as_of_date}\n"
    )
    if lagged:
        header += (
            f"- 注意: 该序列最新观测早于窗口起点（数据滞后），以下为最新可用数据"
            f"（截至 {last_label}）。\n"
        )

    return header + summary + note + table
