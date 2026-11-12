from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.analysts.turn import take_turn
from tradingagents.agents.context import (
    get_instrument_context_from_state,
    get_language_instruction,
    get_past_context_instruction,
)
from tradingagents.agents.tools import (
    get_balance_sheet,
    get_cashflow,
    get_fundamentals,
    get_income_statement,
    get_insider_transactions,
    get_valuation,
    get_announcements,
    get_earnings_guidance,
)

# The tools this analyst is offered; its tool node is built from the same tuple.
TOOLS = (
    get_fundamentals,
    get_valuation,
    get_balance_sheet,
    get_cashflow,
    get_income_statement,
    get_insider_transactions,
    get_announcements,
    get_earnings_guidance,
)


def create_fundamentals_analyst(llm):
    def fundamentals_analyst_node(state):
        current_date = state["trade_date"]
        instrument_context = get_instrument_context_from_state(state)

        system_message = (
            "You are a researcher tasked with analyzing fundamental information over the past week about a company. Please write a comprehensive report of the company's fundamental information such as financial documents, company profile, basic company financials, and company financial history to gain a full view of the company's fundamental information to inform traders. Make sure to include as much detail as possible. Provide specific, actionable insights with supporting evidence to help traders make informed decisions."
            + " Make sure to append a Markdown table at the end of the report to organize key points in the report, organized and easy to read."
            + " Use the available tools: `get_fundamentals` for comprehensive company analysis, `get_valuation` for forward multiples (forward PE/PB, PEG, EV/EBITDA), historical valuation trend, DCF/FCFF estimate and the industry-appropriate valuation model, `get_balance_sheet`, `get_cashflow`, and `get_income_statement` for specific financial statements, `get_insider_transactions` for recent insider buying and selling, `get_announcements` for company disclosures (业绩预告/快报、重大合同、中标、扩产产能、定增回购、增减持、股权激励、分红、并购重组、订单/供货/客户), and `get_earnings_guidance` for the company's own forward earnings guidance (业绩预告预测净利润区间、变动幅度与原因、业绩快报)."
            + " For A-share (Chinese) instruments, leverage the disclosure rhythm to project future earnings and compare against today's valuation: (a) use `get_earnings_guidance` to read the company's own 业绩预告/业绩快报 — the predicted net-profit range, YoY change and the stated reason — as the primary forward earnings anchor; (b) use `get_announcements` to find capacity expansion, major contracts/wins, order/customer news and capex signals that point to future revenue; (c) where a downstream customer's financials and capital expenditure are available (e.g. the company supplies a large customer whose guidance/capex you can infer), use the customer's spend trajectory to *triangulate* the company's future revenue rather than extrapolating only the company's own trailing numbers; (d) convert the resulting forward net-profit estimate into a forward P/E (or the industry-appropriate multiple) and compare it to the current price to judge whether today's value already prices in the guidance — be explicit about whether a gap is a cheap stock or an already-reflected expectation."
            + " When analyzing valuation: (1) choose the valuation model appropriate to the industry — e.g. banks/insurance use PB + ROE (or PEV) rather than PE; high-growth tech/semi uses PEG + PS + forward PE; mature cash-flow-stable consumer uses DCF; asset-heavy industrials use EV/EBITDA — and explain why. (2) Analyze the *trend* of valuation over time and where the current multiple sits in its historical range (percentile), not just the point value. (3) Analyze how the price has moved around its valuation (e.g. earnings-driven vs multiple-expansion-driven moves), and whether a change in price reflects a change in fundamentals or merely a change in the multiple. (4) Distinguish trailing vs forward multiples — forward PE reflects expected future earnings, and a falling forward PE can signal either expected growth or a cheapening stock; be explicit about which. (5) For an ETF, analyze the fundamentals of its main constituent holdings, not the fund wrapper."
            + get_language_instruction()
            + get_past_context_instruction(state)
        )

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " Use the provided tools to progress towards answering the question."
                    " If you are unable to fully answer, that's OK; another assistant with different tools"
                    " will help where you left off. Execute what you can to make progress."
                    " Report what your tools support; another agent decides the trade."
                    " You have access to the following tools: {tool_names}."
                    " Today's date is {current_date}; treat it as 'now' for all analysis and tool-call date ranges. {instrument_context}\n"
                    "{system_message}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(tool_names=", ".join([tool.name for tool in TOOLS]))
        prompt = prompt.partial(current_date=current_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        result, report = take_turn(prompt, llm, TOOLS, state["messages"])

        return {
            "messages": [result],
            "fundamentals_report": report,
        }

    return fundamentals_analyst_node
