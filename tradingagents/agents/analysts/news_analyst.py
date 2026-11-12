from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.analysts.turn import take_turn
from tradingagents.agents.context import get_instrument_context_from_state, get_language_instruction, get_past_context_instruction
from tradingagents.agents.tools import (
    get_cn_macro_indicators,
    get_global_news,
    get_macro_indicators,
    get_news,
    get_prediction_markets,
)
from tradingagents.dataflows.vendors.local_knowledge import fetch_local_knowledge

# The tools this analyst is offered; its tool node is built from the same tuple.
TOOLS = (
    get_news,
    get_global_news,
    get_macro_indicators,
    get_cn_macro_indicators,
    get_prediction_markets,
)


def _macro_weighting_guidance(ticker: str) -> str:
    """Return a macro-weighting instruction based on the instrument's revenue
    geography, or an empty string when it can't be resolved.

    The core rule: a company (or ETF) whose revenue is predominantly domestic is
    driven by China's macro cycle, so weight China macro (PMI/CPI/PPI/GDP/M2/
    industrial policy) heavily and treat US macro as secondary context; the
    reverse for a predominantly-overseas one. Crucially, do NOT linearly
    extrapolate an overseas industry's trend to a domestic-facing Chinese
    company — different demand, policy and capital cycles.
    """
    try:
        from tradingagents.dataflows.vendors.akshare.common import is_cn_market
        from tradingagents.dataflows.vendors.akshare.revenue_geo import describe_revenue_geo

        if not is_cn_market(ticker):
            return ""
        geo = describe_revenue_geo(ticker)
    except Exception:  # noqa: BLE001 — a macro-guidance hiccup must not abort the run
        return ""
    if not geo:
        return ""

    return (
        "\n\n## Macro weighting (revenue-geography driven)\n"
        f"{geo}\n"
        "Weight your macro analysis by this split: if domestic revenue dominates, "
        "lean on **China macro** via get_cn_macro_indicators (e.g. 'pmi', 'cpi', "
        "'ppi', 'gdp', 'm2', 'industrial_production', 'retail_sales', 'lpr') and "
        "treat US/FRED macro (get_macro_indicators) as secondary context only; if "
        "overseas revenue dominates, invert that. **Do not linearly extrapolate an "
        "overseas industry's growth or demand to this instrument's domestic "
        "business** — domestic industrial policy, demand, and capital cycles differ "
        "from overseas and must be assessed on their own data."
    )


def create_news_analyst(llm):
    def news_analyst_node(state):
        current_date = state["trade_date"]
        asset_type = state.get("asset_type", "stock")
        asset_label = "company" if asset_type == "stock" else "asset"
        instrument_context = get_instrument_context_from_state(state)
        ticker = state.get("company_of_interest", "")
        macro_guidance = _macro_weighting_guidance(ticker)

        # 本地知识库（IMA 资料落地）——按公司名 + 行业关键词检索，
        # 命中内容作为专属信息源注入。空则跳过。
        local_knowledge_block = ""
        try:
            local_knowledge_block = fetch_local_knowledge(ticker)
        except Exception:  # noqa: BLE001 — 检索失败不影响主流程
            local_knowledge_block = ""
        local_knowledge_section = ""
        if local_knowledge_block:
            local_knowledge_section = (
                "\n\n## 本地知识库资料（用户自有研报/公众号文章/笔记）\n"
                "以下是检索到与该标的相关的高价值资料，请优先参考其中关于行业产能、"
                "价格走势、估值框架和公司基本面的观点，融入你的新闻与宏观分析中。"
                "注意：这些是观点性资料，需与实时新闻/宏观数据交叉验证。\n\n"
                f"{local_knowledge_block}\n"
            )

        system_message = (
            f"You are a news researcher tasked with analyzing recent news and trends over the past week. Please write a comprehensive report of the current state of the world that is relevant for trading and macroeconomics. Use the available tools: get_news(start_date, end_date) for news about the {asset_label} under analysis, get_global_news(curr_date, look_back_days, limit) for broader macroeconomic news, get_macro_indicators(indicator, curr_date, look_back_days) to ground macro commentary in actual data from FRED (e.g. 'cpi', 'core_pce', 'unemployment', 'fed_funds_rate', '10y_treasury', 'yield_curve'), get_cn_macro_indicators(indicator, curr_date, look_back_days) for China macro (e.g. 'pmi', 'cpi', 'ppi', 'gdp', 'm2', 'industrial_production', 'retail_sales', 'lpr'), and get_prediction_markets(topic, limit) for live market-implied probabilities of forward-looking events (e.g. 'Fed rate cut', 'recession 2026', geopolitical or sector events). Provide specific, actionable insights with supporting evidence to help traders make informed decisions."
            + """ Make sure to append a Markdown table at the end of the report to organize key points in the report, organized and easy to read."""
            + local_knowledge_section
            + macro_guidance
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
            "news_report": report,
        }

    return news_analyst_node
