"""AkShare data vendors for Chinese equities (A-shares / HK).

AkShare (https://akshare.akfamily.xyz) is a free, open-source library that
aggregates public Chinese financial data sources (Eastmoney, Sina, Juchao,
the exchanges). It needs no API key, which makes it the natural primary data
source for instruments that the US-centric vendors (yfinance, Alpha Vantage,
SEC EDGAR, FRED, Finnhub) do not cover well: Shanghai/Shenzhen A-shares and
Hong Kong equities.

Each function mirrors the signature of its Yahoo/Alpha Vantage counterpart so
it can drop into the router's vendor chain unchanged.
"""

from tradingagents.dataflows.vendors.akshare.stock import get_stock
from tradingagents.dataflows.vendors.akshare.fundamentals import get_fundamentals
from tradingagents.dataflows.vendors.akshare.news import get_news, get_global_news
from tradingagents.dataflows.vendors.akshare.valuation import get_valuation, get_etf_fundamentals
from tradingagents.dataflows.vendors.akshare.statements import (
    get_balance_sheet,
    get_cashflow,
    get_income_statement,
)
from tradingagents.dataflows.vendors.akshare.announcements import (
    get_announcements,
    get_earnings_guidance,
)
from tradingagents.dataflows.vendors.akshare.research import (
    get_downstream_analysis,
    get_research_reports,
)

__all__ = [
    "get_stock",
    "get_fundamentals",
    "get_news",
    "get_global_news",
    "get_valuation",
    "get_etf_fundamentals",
    "get_income_statement",
    "get_balance_sheet",
    "get_cashflow",
    "get_announcements",
    "get_earnings_guidance",
    "get_research_reports",
    "get_downstream_analysis",
]
