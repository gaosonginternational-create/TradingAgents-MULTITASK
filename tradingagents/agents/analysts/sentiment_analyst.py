"""Sentiment analyst: one sentiment report from four sources.

The node fetches its sources before calling the model and puts them in the
prompt, so the model reports on data it was given rather than inventing posts:

  1. News headlines: Yahoo Finance
  2. SentiSense: model-scored retail sentiment (Reddit/X/YouTube/Substack/news)
  3. StockTwits messages: the cashtag stream, with Bullish/Bearish tags
  4. Reddit posts: r/wallstreetbets, r/stocks, r/investing, or crypto communities for a crypto pair

Each source is trimmed to the analysis window. With a TypeSafe key, the social
posts are screened by Jev first (see post_screen). These feeds serve recent items
and are not archived, so a historical run's sentiment inputs are not
point-in-time.

The report is a SentimentReport through structured output where the provider
supports it and free text otherwise, so the band, score and confidence header
reads the same across providers.
"""

from datetime import datetime, timedelta

from langchain_core.messages import AIMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.context import get_instrument_context_from_state, get_language_instruction
from tradingagents.agents.post_screen import jev_screen
from tradingagents.agents.schemas import SentimentReport, render_sentiment_report
from tradingagents.agents.structured import (
    NO_EXTERNAL_TOOLS,
    bind_structured,
    invoke_structured_or_freetext,
)
from tradingagents.agents.tools import get_news
from tradingagents.dataflows.vendors.reddit import (
    CRYPTO_SUBREDDITS,
    DEFAULT_SUBREDDITS,
    fetch_reddit_posts,
    subreddits_for,
)
from tradingagents.dataflows.vendors.stocktwits import fetch_stocktwits_messages
from tradingagents.dataflows.vendors.sentisense import fetch_sentisense_sentiment
from tradingagents.dataflows.vendors.akshare.common import is_cn_market
from tradingagents.dataflows.vendors.akshare.sentiment import get_cn_sentiment
from tradingagents.dataflows.vendors.akshare.moneyflow import get_ths_moneyflow
from tradingagents.dataflows.vendors.akshare.orderbook import get_orderbook
from tradingagents.dataflows.vendors.akshare.national_team import get_national_team_flow
from tradingagents.dataflows.vendors.akshare.wechat import get_wechat_news
from tradingagents.dataflows.vendors.fingpt import get_fingpt_sentiment


def _extract_news_headlines(news_block: str, limit: int = 15) -> list[str]:
    """Pull plain headline text out of the markdown news block for FinGPT scoring.

    The news vendors render headlines as ``### <title>`` lines (possibly with a
    ``(source: …)`` suffix). FinGPT-Sentiment wants bare headline text, so we
    strip the markdown prefix and source suffix and return at most ``limit``.
    """
    headlines = []
    for line in (news_block or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("### "):
            continue
        title = stripped[4:].strip()
        if not title:
            continue
        # 去掉可能的 " (source: …)" 后缀
        if " (source:" in title:
            title = title.split(" (source:", 1)[0].strip()
        if title and title not in headlines:
            headlines.append(title)
        if len(headlines) >= limit:
            break
    return headlines


def _seven_days_back(trade_date: str) -> str:
    return (datetime.strptime(trade_date, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")


def create_sentiment_analyst(llm):
    """Create a sentiment analyst node for the trading graph.

    Pre-fetches news + StockTwits + Reddit data, injects them into the
    prompt as structured blocks, and produces a deterministic sentiment
    report via structured output (with a free-text fallback for providers
    that do not support it).
    """
    structured_llm = bind_structured(llm, SentimentReport, "Sentiment Analyst")

    def sentiment_analyst_node(state):
        ticker = state["company_of_interest"]
        end_date = state["trade_date"]
        start_date = _seven_days_back(end_date)
        instrument_context = get_instrument_context_from_state(state)

        # Pre-fetch all sources. Each fetcher degrades gracefully and
        # returns a string (no exceptions surface from here), so the LLM
        # always sees something — either real data or a clear placeholder.
        news_block = get_news.func(ticker, start_date, end_date)
        # Pass the analysis window so a historical run trims social posts to it
        # instead of leaking today's chatter into a backtest (#1220).
        screen = jev_screen(ticker)
        stocktwits_block = fetch_stocktwits_messages(
            ticker, limit=30, start_date=start_date, end_date=end_date, screen=screen
        )
        sentisense_block = fetch_sentisense_sentiment(
            ticker, start_date=start_date, end_date=end_date
        )
        subreddits = subreddits_for(ticker)
        reddit_block = fetch_reddit_posts(
            ticker, subreddits, start_date=start_date, end_date=end_date, screen=screen
        )
        # Domestic (A-share/HK/ETF) tickers get an Eastmoney sentiment block:
        # the US sources (StockTwits/SentiSense/Reddit) don't cover them, so a
        # domestic read replaces those placeholders with real signal.
        cn_market = is_cn_market(ticker)
        cn_sentiment_block = ""
        cn_moneyflow_block = ""
        cn_orderbook_block = ""
        national_team_block = ""
        cn_wechat_block = ""
        if cn_market:
            try:
                cn_sentiment_block = get_cn_sentiment(ticker)
            except Exception:
                cn_sentiment_block = ""
            try:
                cn_moneyflow_block = get_ths_moneyflow(ticker)
            except Exception:
                cn_moneyflow_block = ""
            try:
                cn_orderbook_block = get_orderbook(ticker)
            except Exception:
                cn_orderbook_block = ""
            # 国家队宽基 ETF 动向是市场级背景，对任何国内标的都有参考价值
            try:
                national_team_block = get_national_team_flow(end_date)
            except Exception:
                national_team_block = ""
            # 微信公众号观点（搜狗微信搜索）——专业媒体/大V 的舆情补充
            try:
                cn_wechat_block = get_wechat_news(ticker)
            except Exception:
                cn_wechat_block = ""
        # FinGPT local-inference sentiment scoring of the news headlines — an
        # independent, model-based read that works for both US and domestic
        # news. Degrades to a placeholder if the local service is down.
        fingpt_sentiment_block = ""
        try:
            headlines = _extract_news_headlines(news_block)
            fingpt_sentiment_block = get_fingpt_sentiment(headlines)
        except Exception:
            fingpt_sentiment_block = ""

        system_message = _build_system_message(
            ticker=ticker,
            start_date=start_date,
            end_date=end_date,
            news_block=news_block,
            stocktwits_block=stocktwits_block,
            sentisense_block=sentisense_block,
            reddit_block=reddit_block,
            subreddits=subreddits,
            cn_market=cn_market,
            cn_sentiment_block=cn_sentiment_block,
            cn_moneyflow_block=cn_moneyflow_block,
            cn_orderbook_block=cn_orderbook_block,
            national_team_block=national_team_block,
            cn_wechat_block=cn_wechat_block,
            fingpt_sentiment_block=fingpt_sentiment_block,
        )

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " Report what your tools support; another agent decides the trade."
                    # No tool-calling here: the data is pre-fetched into the
                    # prompt, so tool-range wording would only invite a
                    # hallucinated tool call (#1130).
                    " Today's date is {current_date}; treat it as 'now' for all analysis. {instrument_context}"
                    " " + NO_EXTERNAL_TOOLS +
                    "\n{system_message}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(current_date=end_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        # Format the template into a concrete message list so the structured
        # and free-text paths receive the same input. No bind_tools — the
        # data is already in the prompt.
        formatted_messages = prompt.format_messages(messages=state["messages"])

        report_text = invoke_structured_or_freetext(
            structured_llm,
            llm,
            formatted_messages,
            render_sentiment_report,
            "Sentiment Analyst",
        )

        return {
            "messages": [AIMessage(content=report_text)],
            "sentiment_report": report_text,
        }

    return sentiment_analyst_node


def _build_system_message(
    *,
    ticker: str,
    start_date: str,
    end_date: str,
    news_block: str,
    stocktwits_block: str,
    sentisense_block: str,
    reddit_block: str,
    subreddits: tuple[str, ...] = DEFAULT_SUBREDDITS,
    cn_market: bool = False,
    cn_sentiment_block: str = "",
    cn_moneyflow_block: str = "",
    cn_orderbook_block: str = "",
    national_team_block: str = "",
    cn_wechat_block: str = "",
    fingpt_sentiment_block: str = "",
) -> str:
    """Assemble the sentiment-analyst system message with structured data blocks."""
    if subreddits == DEFAULT_SUBREDDITS:
        character = "r/wallstreetbets is often contrarian/exuberant; r/stocks more measured; r/investing longer-term"
    elif len(subreddits) > len(CRYPTO_SUBREDDITS):
        character = f"r/{subreddits[0]} leans toward the coin's holders; r/CryptoCurrency and r/CryptoMarkets are broader"
    else:
        character = "r/CryptoCurrency and r/CryptoMarkets are broad crypto communities"

    # Domestic tickers get an Eastmoney block and adjusted guidance; US tickers
    # keep the SentiSense/StockTwits/Reddit analysis as-is.
    if cn_market:
        cn_block = f"""### 国内散户情绪 — 东方财富（千股千评 / 人气榜 / 热搜概念）
A machine-readable domestic retail-sentiment read. 综合得分 (composite score, 0–100, higher = more bullish/positive), 关注指数 (attention index, higher = more attention), 人气排名 (popularity rank, lower = hotter), 机构参与度 (institutional participation), and 关注度变化 (attention trend).

<start_of_cn_sentiment>
{cn_sentiment_block}
<end_of_cn_sentiment>

### 国内资金流向 — 同花顺（主力资金净流入 / 流出 / 净额）
A machine-readable domestic capital-flow read. 流入资金/流出资金/净额 (亿元), 净流入占比 (净额占成交额比例), 换手率, and 涨跌幅. This is *money*, not opinion: 主力 (institutional / large-order) net inflow vs 散户 net outflow are mechanically related, so the key signal is the *direction and magnitude* of 净额 and its sign.

<start_of_cn_moneyflow>
{cn_moneyflow_block}
<end_of_cn_moneyflow>

### 五档盘口 — 东财（买一~买五 / 卖一~卖五 / 委比）
A five-level order-book snapshot: 买一~买五/卖一~卖五 prices and resting volumes, the 买卖价差 (spread), 委比 (bid-ask imbalance), and total resting bid/ask volume. This reads *intraday* supply/demand pressure — a coarser but immediate read of who is pushing at the margin right now. Resting orders can be cancelled, so treat it as a short-term lean, not a hard signal.

<start_of_cn_orderbook>
{cn_orderbook_block}
<end_of_cn_orderbook>

### 国家队宽基 ETF 动向 — 主力资金流 + 份额变化
The "national team" (中央汇金/证金/社保) funnels support through broad-index ETFs (沪深300/上证50/科创50/中证500/中证1000/创业板). Each ETF's 当日/10日/20日 主力净流入 and (Shenzhen) 份额净增减 is a direct read of whether the national team is buying or selling this week/month. Use the 10日/20日累计 to distinguish a one-day blip from a sustained stance.

<start_of_national_team>
{national_team_block}
<end_of_national_team>

### 微信公众号观点 — 搜狗微信搜索（专业媒体 / 大V 舆情）
WeChat Official Account articles surfaced by keyword search (Sogou WeChat search). Each item is a *headline + short summary* from financial/media accounts — a professional-opinion and narrative read that the retail feeds (千股千评) and news feeds (Eastmoney/Caixin) don't carry. It reflects how the media/大V are *framing* the stock, not raw retail sentiment. Note the search returns a small, recent sample (not exhaustive), and only headline+summary (full text is not retrievable). If the block is `<unavailable>`, the search was blocked/empty — treat it as an optional signal, not a data gap.

<start_of_cn_wechat>
{cn_wechat_block}
<end_of_cn_wechat>

## How to analyze this domestic data

1. **Read 综合得分 as the primary directional signal.** A score ≥ 80 is clearly positive, 50–80 neutral-to-positive, < 50 negative. 关注指数 ≥ 80 means heavy retail attention (which can signal either genuine interest or crowded/contrarian risk — flag which). 人气排名 in the top 10 is very hot; outside the top 100 is cool.
2. **Read 资金净额 as the money-flow confirmation/divergence signal.** 净额 > 0 means 主力 net buying; < 0 means 主力 net selling. Use 净流入占比 (净额/成交额) to judge magnitude — |占比| ≥ 5% is meaningful, ≥ 10% is strong.
3. **Flag the sentiment-vs-moneyflow divergence.** The single most useful domestic read: 情绪看多 (综合得分高) 但 资金净流出 = 散户追高、主力派发（危险）；情绪看空 但 资金净流入 = 主力吸筹（潜在机会）。These divergences are contrarian signals and must be called out explicitly.
4. **Watch the divergence between 综合得分 and 涨跌幅/最新价.** A falling price with a rising 关注指数 and 综合得分 often marks retail dip-buying; a rising price with a cooling 关注指数 can mark distribution.
5. **机构参与度 matters.** Higher institutional participation means the sentiment read is more "smart money"-driven, less pure retail noise.
6. **Read 委比 as the short-term intraday lean.** 委比 > +0.2 means the order book is bid-heavy (near-term support), < -0.2 ask-heavy (near-term pressure). Cross-check it against 资金净额: a bid-heavy book with 主力净流出 can mean the big orders are selling into retail bids.
7. **Read the 国家队 ETF 动向 as the market-level background.** If broad-index ETFs show sustained 10日/20日 净流入 (especially 沪深300/上证50/科创50), that is structural support — a tailwind for the broader market and for liquid large-caps. Sustained 净流出 is a headwind. This is context, not a per-ticker call.
8. **The US social sources (SentiSense/StockTwits/Reddit) do not cover this domestic ticker** — treat their placeholders as expected absences, not data gaps, and lean on this domestic block plus news.
9. **Read the 微信公众号 block as the narrative/media-framing signal.** The account headlines show how professional media and 大V are framing the stock right now (e.g. "业绩超预期", "估值承压", "政策利好"). It is *opinion/narrative*, distinct from 千股千评 (retail mood) and 资金流 (money). Flag a divergence: 媒体一致看多但主力资金净流出 = 情绪/叙事与资金背离，警惕追高；媒体看空但资金流入 = 可能错杀。Use it to gauge *what story the market is telling*, not as a buy/sell signal by itself.
"""
        cn_rules = "3. **For domestic tickers, weight the 东方财富 block (综合得分/关注指数), the 同花顺资金流 block (净额/净流入占比), and the 盘口委比 as the primary signals** — and above all flag the sentiment-vs-moneyflow divergence. Use the 微信公众号 block as the media-narrative read, and the 国家队宽基ETF动向 as market-level context. The US sources will be unavailable placeholders; that is expected."
    else:
        cn_block = ""
        cn_rules = "3. **Look for cross-source divergences.** If SentiSense is bearish but StockTwits is overwhelmingly bullish (or news framing disagrees), that mismatch is itself a signal — model-weighted vs user-labelled reads can diverge when retail is chasing a thesis the models discount."

    return f"""You are a financial market sentiment analyst. Your task is to produce a comprehensive sentiment report for {ticker} covering the period from {start_date} to {end_date}, drawing on multiple complementary data sources that have already been collected for you.

## Data sources (pre-fetched, in this prompt)

### News headlines — past 7 days
Institutional framing. Fact-driven, slower-moving signal.

<start_of_news>
{news_block}
<end_of_news>

### FinGPT — local model-scored headline sentiment
An independent financial-LLM read of the news headlines above. Each headline
is classified positive/negative/neutral by the local FinGPT-Sentiment model
(Llama2-13B LoRA). This is a *model* score (like SentiSense) but runs locally
and covers whatever headlines the news vendor returned — including domestic
headlines the US social vendors miss.

<start_of_fingpt_sentiment>
{fingpt_sentiment_block}
<end_of_fingpt_sentiment>

### SentiSense — model-scored retail sentiment (Reddit / X / YouTube / Substack / news)
A signed sentiment polarity (-1.0 bearish to +1.0 bullish), a mention-volume count, and a proprietary bull/bear score, all scored by financial-text models rather than user labels. This is a cleaner, model-weighted retail sentiment signal.

<start_of_sentisense>
{sentisense_block}
<end_of_sentisense>

### StockTwits messages — retail-trader social platform indexed by cashtag
Fast-moving signal. Each message carries a user-labeled sentiment tag (Bullish / Bearish / no-label) plus the message body.

<start_of_stocktwits>
{stocktwits_block}
<end_of_stocktwits>

### Reddit posts — {", ".join(f"r/{sub}" for sub in subreddits)} (past 7 days)
Community discussion, without vote or comment counts. Subreddit character matters ({character}).

<start_of_reddit>
{reddit_block}
<end_of_reddit>
{cn_block}
## How to analyze this data (best practices)

1. **Read the SentiSense polarity and bull/bear score as the primary retail-sentiment signal.** It is model-weighted and already normalized to a signed scale, so it is the most reliable directional read. A polarity ≥ +0.05 is bullish, ≤ -0.05 bearish, otherwise neutral. The mention volume tells you whether the read is backed by meaningful activity or just noise.

2. **Read the StockTwits Bullish/Bearish ratio as a secondary, user-labelled signal.** A 70/30 bullish/bearish split is moderately bullish; ≥90/10 may indicate over-extension and contrarian risk; 50/50 is uncertainty. Sample size matters — base rates on the actual message count, not percentages alone. A block headed "Screened by Jev" has had off-topic posts removed.

{cn_rules}

4. **Read Reddit posts for substance.** The feed carries no vote or comment counts, so judge a post by its body excerpt, not its title alone, and do not infer engagement.

5. **Read the FinGPT headline scores as a model-level cross-check on news framing.** It independently classifies each headline's tone. If the FinGPT headline mix is clearly positive while SentiSense is negative (or vice versa), that divergence is meaningful: one reflects news framing, the other reflects retail chatter. If the FinGPT block is `<unavailable>`, the local service is simply not running — treat it as an optional signal, not a data gap.

5. **Distinguish opinion from event.** A news headline is an event; a social post is opinion. Both are inputs but should be weighted differently in your conclusions.

6. **Identify recurring narrative themes.** What topic keeps coming up across sources? That's the dominant narrative driving current sentiment.

7. **Be honest about data limits.** If one or more sources returned an "<unavailable>" placeholder (StockTwits is often unavailable since its public API was retired), the sentiment read is less robust — flag this explicitly in the `confidence` field and the narrative, and lean on whichever sources did return data.

8. **Identify catalysts and risks** that emerge across sources — news of upcoming earnings, product launches, competitive threats, macro headlines, etc.

9. **Past sentiment is not predictive.** Frame your conclusions as signal for the trader to weigh alongside fundamentals and technicals, not as a price call.

## Output fields

Fill the following fields:

- **overall_band**: Exactly one of Bullish / Mildly Bullish / Neutral / Mixed / Mildly Bearish / Bearish. Use Mixed when sources point in clearly different directions; Neutral only when all sources are genuinely silent.
- **overall_score**: A number from 0 (maximally bearish) to 10 (maximally bullish); 5 is neutral. Keep it consistent with overall_band.
- **confidence**: low / medium / high, based on data quality and sample size.
- **narrative**: Full source-by-source breakdown, divergences, dominant narrative themes, catalysts and risks, and a markdown summary table of key sentiment signals (direction, source, supporting evidence).

{get_language_instruction()}"""
