"""Local view of the user's ima knowledge base, as an analyst data source.

ima (the user's personal knowledge base) holds the industry/macro/news corpus
they curate: 财联社 电报, 芯榜/芯智讯/半导体行业观察/梓豪谈芯 的深度文章, 36氪, 研报
PDF/PPT/Excel. That corpus is exactly what a fundamentals/news read needs and
what free feeds lack: supply-chain detail, capacity/capex talk, industry-cycle
commentary and macro policy.

The container cannot call ima's MCP server, so the corpus is *synced* into a
local store (``data/ima/store.jsonl``) by ``tools/ima_sync.py`` and read from
there. This module turns that store into four analyst-facing views:

  - 公司自身 / 上下游与供应链 / 行业与周期 / 宏观与政策
  - 趋势变化 (items ordered in time, with direction words flagged)
  - 复盘 (past predictions + whether they were verified, from the ledger)

Retrieval is deliberately local and deterministic: keyword scoring over the
ticker's aliases plus the section vocabulary, de-duplicated by title (the same
article is often filed in several ima folders), recency-weighted, and capped so
one verbose source cannot crowd out the rest.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime

from tradingagents.dataflows.errors import NoMarketDataError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Store location. Synced files live under the data dir so they travel with the
# reports and are visible to the user.
# ---------------------------------------------------------------------------
def _data_root() -> str:
    """Where the ima store lives, on the host and inside the container.

    On the host the corpus is written to ``<repo>/data/ima``; in the container
    ``<repo>/data`` is mounted at ``~/.tradingagents``, so the same files appear
    there. Resolve by what actually exists rather than by guessing the
    environment, so a synced store is picked up in both places.
    """
    env = os.getenv("TRADINGAGENTS_DATA_DIR")
    repo_data = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))))), "data")
    home = os.path.expanduser(os.path.join("~", ".tradingagents"))

    # Home (the container's mount) is checked before the repo copy: an image
    # that happens to carry a stale data/ must never shadow the live volume.
    candidates = [c for c in (env, home, repo_data) if c]
    for c in candidates:
        if os.path.exists(os.path.join(c, "ima", "store.jsonl")):
            return c
    for c in candidates:
        # A real data home has been used at least once (logs/cache exist).
        if os.path.isdir(os.path.join(c, "logs")) or os.path.isdir(os.path.join(c, "cache")):
            return c
    return repo_data


def store_path() -> str:
    return os.path.join(_data_root(), "ima", "store.jsonl")


def ledger_path() -> str:
    return os.path.join(_data_root(), "ima", "ledger.jsonl")


# ---------------------------------------------------------------------------
# Section vocabularies. An article usually touches several; the first matching
# bucket wins for grouping, but its text is still searchable by all terms.
# ---------------------------------------------------------------------------
SECTION_KEYWORDS: dict[str, tuple[str, ...]] = {
    "上下游与供应链": (
        "上游", "下游", "供应链", "产业链", "客户", "订单", "产能", "扩产", "稼动率",
        "晶圆", "代工", "封测", "设备", "材料", "光刻", "存储", "DRAM", "NAND", "HBM",
        "CoWoS", "先进封装", "良率", "缺货", "交付", "库存", "备货", "拉货",
    ),
    "行业与周期": (
        "行业", "市场", "份额", "景气", "周期", "涨价", "降价", "价格", "毛利率",
        "供给", "需求", "渗透率", "国产替代", "竞争格局", "出货", "销量", "招标",
    ),
    "宏观与政策": (
        "美联储", "利率", "降息", "加息", "关税", "汇率", "CPI", "PPI", "PMI", "GDP",
        "央行", "财政", "政策", "监管", "证监会", "补贴", "出口管制", "制裁", "实体清单",
        "国债", "流动性", "社融", "M2",
    ),
}

# Words that mark a *change* rather than a level — what the user asked to watch.
TREND_WORDS = (
    "首次", "首次突破", "上调", "下调", "上修", "下修", "超预期", "不及预期",
    "创新高", "创新低", "转正", "转负", "加速", "放缓", "回暖", "走弱", "复苏",
    "扩产", "减产", "涨价", "跌价", "缺货", "过剩", "收紧", "放松", "转向",
)

# Ticker -> the names/keywords a relevant article is likely to use. Kept small
# and explicit: a wrong alias pulls in noise, which is worse than a miss.
TICKER_ALIASES: dict[str, tuple[str, ...]] = {
    # --- A 股 / ETF ---
    "002371": ("北方华创", "Naura", "半导体设备", "刻蚀", "薄膜沉积", "炉管", "清洗设备"),
    "002475": ("立讯精密", "立讯", "连接器", "果链", "AirPods", "铜缆", "组装"),
    "159530": ("机器人", "人形机器人", "人形", "减速器", "伺服", "灵巧手", "执行器"),
    "159852": ("软件", "信创", "SaaS", "国产软件", "工业软件"),
    "300308": ("中际旭创", "旭创", "光模块", "800G", "1.6T", "CPO", "硅光"),
    "588170": ("半导体", "芯片", "晶圆", "先进制程", "科创板", "集成电路"),
    "600999": ("招商证券", "券商", "证券", "经纪", "两融", "投行"),
    "601939": ("建设银行", "建行", "银行", "净息差", "息差", "不良率"),
    "603186": ("华正新材", "覆铜板", "CCL", "高频高速", "树脂", "PCB"),
    "603259": ("药明康德", "药明", "CXO", "CRO", "CDMO"),
    "688008": ("澜起科技", "澜起", "内存接口", "DDR5", "MRCD", "MDB", "CKD", "PCIe Retimer"),
    "603986": ("兆易创新", "兆易", "NOR Flash", "MCU", "存储"),
    "688102": ("斯瑞新材", "斯瑞", "铜合金", "触头", "CT球管", "轨交"),
    "002415": ("海康威视", "海康", "安防"),
    "300750": ("宁德时代", "宁德", "动力电池", "储能"),
    "600519": ("贵州茅台", "茅台", "白酒"),
    "600900": ("长江电力", "水电", "来水", "三峡", "乌东德", "白鹤滩"),
    "000338": ("潍柴动力", "潍柴", "重卡", "发动机", "发电机组", "大缸径", "KION", "叉车"),
    # --- 美股 / 海外 ---
    "AAPL": ("苹果", "Apple", "iPhone", "果链"),
    "AMD": ("AMD", "Instinct", "EPYC", "MI350", "苏姿丰", "Lisa Su"),
    "GLW": ("康宁", "Corning", "光纤", "玻璃基板"),
    "INTC": ("英特尔", "Intel", "至强", "18A", "代工"),
    "LITE": ("Lumentum", "光模块", "光通信", "VCSEL", "EML"),
    "META": ("Meta", "广告", "资本开支", "元宇宙", "AI 基建"),
    "MRVL": ("Marvell", "迈威尔", "定制芯片", "ASIC", "光DSP"),
    "MU": ("美光", "Micron", "DRAM", "NAND", "HBM"),
    "NVDA": ("英伟达", "Nvidia", "黄仁勋", "Blackwell", "GB200", "GPU"),
    "SKHY": ("SK海力士", "海力士", "HBM", "DRAM", "存储"),
    "SNDK": ("闪迪", "SanDisk", "NAND", "SSD", "存储"),
    "SOXX": ("费城半导体", "半导体指数", "SOX", "芯片股"),
}


# --- 行业/上下游相关度 -----------------------------------------------------
# 一条讲"重卡销量下滑""DDR5 渗透加快""覆铜板涨价"的文章，即使没点名公司，
# 对该标的也是有用的一手产业信息。只按公司别名过滤会把这类内容全部丢掉，
# 所以再建一层"行业关键词"：命中即纳入，但标注为"行业/上下游相关（未点名公司）"，
# 让分析师明白这是间接证据、不能当作公司层面的确认。
INDUSTRY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "semi_equipment": ("半导体设备", "刻蚀", "薄膜沉积", "炉管", "清洗设备", "晶圆厂",
                       "扩产", "资本开支", "设备招标", "国产替代"),
    "memory_interface": ("内存接口", "DDR5", "DDR4", "服务器", "数据中心", "DRAM",
                         "内存条", "模组", "CKD", "MRDIMM"),
    "optical": ("光模块", "800G", "400G", "1.6T", "CPO", "硅光", "光通信", "光芯片", "EML"),
    "ai_compute": ("AI 算力", "AI算力", "GPU", "加速卡", "数据中心", "资本开支", "推理",
                   "训练", "HBM", "英伟达", "算力租赁"),
    "heavy_truck": ("重卡", "重卡销量", "发动机", "工程机械", "天然气重卡", "国七",
                    "排放标准", "基建", "物流", "发电机组", "大缸径", "船机", "叉车"),
    "power_utility": ("水电", "来水", "电价", "电力", "发电量", "利用小时", "抽水蓄能"),
    "bank": ("银行", "净息差", "息差", "信贷", "不良率", "存款", "贷款", "LPR"),
    "brokerage": ("券商", "证券", "经纪", "两融", "投行", "IPO", "成交额"),
    "cxo": ("CXO", "CRO", "CDMO", "创新药", "医药外包", "订单", "产能"),
    "pcb_material": ("覆铜板", "CCL", "PCB", "高频高速", "树脂", "铜箔", "服务器板"),
    "robotics": ("机器人", "人形机器人", "减速器", "伺服", "灵巧手", "执行器", "丝杠"),
    "software": ("软件", "信创", "SaaS", "工业软件", "国产软件", "操作系统"),
    "consumer_electronics": ("果链", "iPhone", "消费电子", "组装", "连接器", "精密制造"),
    "glass_optical": ("玻璃基板", "面板", "光纤", "光缆", "显示"),
    "storage": ("NAND", "DRAM", "HBM", "存储", "闪存", "SSD", "颗粒", "涨价", "库存"),
    "semiconductor": ("半导体", "芯片", "晶圆", "制程", "先进封装", "CoWoS", "集成电路"),
}

TICKER_INDUSTRY: dict[str, tuple[str, ...]] = {
    "002371": ("semi_equipment", "semiconductor"),
    "002475": ("consumer_electronics", "optical"),
    "159530": ("robotics",),
    "159852": ("software",),
    "300308": ("optical", "ai_compute"),
    "588170": ("semiconductor", "semi_equipment"),
    "600999": ("brokerage",),
    "601939": ("bank",),
    "603186": ("pcb_material", "semiconductor"),
    "603259": ("cxo",),
    "688008": ("memory_interface", "ai_compute", "semiconductor"),
    "603986": ("storage", "semiconductor"),
    "688102": ("semi_equipment",),
    "002415": ("robotics", "semiconductor"),
    "300750": ("storage",),
    "600519": ("consumer_electronics",),
    "600900": ("power_utility",),
    "000338": ("heavy_truck", "ai_compute"),
    "AAPL": ("consumer_electronics",),
    "AMD": ("ai_compute", "semiconductor"),
    "GLW": ("glass_optical", "optical"),
    "INTC": ("semiconductor", "ai_compute"),
    "LITE": ("optical",),
    "META": ("ai_compute",),
    "MRVL": ("ai_compute", "optical", "semiconductor"),
    "MU": ("storage", "ai_compute"),
    "NVDA": ("ai_compute", "semiconductor"),
    "SKHY": ("storage", "ai_compute"),
    "SNDK": ("storage",),
    "SOXX": ("semiconductor",),
}

_MAX_ITEMS = 18          # total items injected
_MAX_PER_SECTION = 6     # per bucket, so one theme cannot dominate


def _norm_title(title: str) -> str:
    """A de-dup key: punctuation/spacing stripped so the same article collapses."""
    return re.sub(r"[\s\W_]+", "", (title or "").lower())


def load_store() -> list[dict]:
    """All synced ima items, newest first. Empty list when nothing was synced."""
    path = store_path()
    if not os.path.exists(path):
        return []
    items = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as exc:
        logger.warning("ima store unreadable: %s", exc)
        return []
    items.sort(key=lambda x: x.get("published") or "", reverse=True)
    return items


def _aliases(ticker: str) -> tuple[str, ...]:
    key = (ticker or "").strip().upper()
    return TICKER_ALIASES.get(key, TICKER_ALIASES.get(ticker, ()))


def _score(item: dict, aliases: tuple[str, ...], as_of: str | None) -> float:
    """Relevance of one item: alias hits weigh most, then section vocabulary."""
    title = item.get("title") or ""
    text = item.get("text") or ""
    score = 0.0
    for alias in aliases:
        if alias and alias.lower() in title.lower():
            score += 6.0
        elif alias and alias.lower() in text.lower():
            score += 2.0
    for words in SECTION_KEYWORDS.values():
        for w in words:
            if w in title:
                score += 1.0
            elif w in text:
                score += 0.2
    for w in TREND_WORDS:
        if w in title:
            score += 1.5
    published = item.get("published") or ""
    if as_of and published:
        # Recency: an item inside the window is worth more than an old one.
        if published[:10] <= as_of:
            score += 2.0
        else:
            score -= 20.0  # never leak post-analysis-date text into a backtest
    return score


def _alias_hits(item: dict, aliases: tuple[str, ...]) -> int:
    """How many of the ticker's aliases appear in this item's title or body."""
    title = (item.get("title") or "").lower()
    text = (item.get("text") or "").lower()
    n = 0
    for a in aliases:
        low = a.lower()
        if low and (low in title or low in text):
            n += 1
    return n


def _industry_hits(item: dict, ticker: str) -> int:
    """How many of the ticker's *industry* keywords appear in this item."""
    groups = TICKER_INDUSTRY.get((ticker or "").strip().upper(), TICKER_INDUSTRY.get(ticker, ()))
    if not groups:
        return 0
    title = (item.get("title") or "")
    text = (item.get("text") or "")
    n = 0
    for g in groups:
        for w in INDUSTRY_KEYWORDS.get(g, ()):
            if w in title:
                n += 2
            elif w in text:
                n += 1
    return n


def _bucket(item: dict, aliases: tuple[str, ...]) -> str:
    """Best-fit section for an item (title hits first, then body hits)."""
    title = item.get("title") or ""
    text = item.get("text") or ""
    for alias in aliases:
        if alias and alias.lower() in title.lower():
            return "公司自身"
    for name, words in SECTION_KEYWORDS.items():
        if any(w in title for w in words):
            return name
    for name, words in SECTION_KEYWORDS.items():
        if any(w in text for w in words):
            return name
    return "公司自身" if any(a and a.lower() in text.lower() for a in aliases) else "其他"


def _trend_flags(text: str) -> list[str]:
    return [w for w in TREND_WORDS if w in (text or "")]


def _fmt_item(item: dict, tag: str = "") -> str:
    published = (item.get("published") or "")[:10] or "日期未知"
    source = item.get("source") or "ima"
    title = (item.get("title") or "").strip()
    body = re.sub(r"\s+", " ", item.get("text") or "").strip()
    flags = _trend_flags(title + " " + body[:400])
    head = f"- [{published}] {title}（{source}）{tag}"
    if flags:
        head += "  ⚑趋势信号: " + "/".join(flags[:4])
    excerpt = body[:260] + ("…" if len(body) > 260 else "")
    return head + (f"\n    {excerpt}" if excerpt else "")


def get_ima_knowledge(
    ticker: str,
    as_of_date: str | None = None,
    *,
    limit: int = _MAX_ITEMS,
    focus: str | None = None,
) -> str:
    """Render the synced ima corpus for ``ticker`` as an analyst brief.

    Returns a markdown block grouped into 公司自身 / 上下游与供应链 / 行业与周期 /
    宏观与政策, with trend words flagged and a 复盘 (prediction scorecard) section
    from the ledger. Raises NoMarketDataError when nothing has been synced so the
    analyst reports an honest gap instead of inventing coverage.

    ``focus="industry"`` 是给市场分析师用的：只看**产业整体**那一层——行业与周期、
    上下游与供应链，以及研报类来源（行业展望、产能与价格跟踪、季度策略）。这类
    文字通常不点名个股，但正是判断"这波涨跌是行业景气还是个股故事"的依据。
    """
    items = load_store()
    if not items:
        raise NoMarketDataError(
            ticker, ticker,
            "ima 知识库尚未同步（data/ima/store.jsonl 不存在或为空）；"
            "运行 tools/ima_sync.py 后再分析",
        )

    aliases = _aliases(ticker)
    scored = [(it, _score(it, aliases, as_of_date)) for it in items]
    scored = [(it, s) for it, s in scored if s > 0]
    scored.sort(key=lambda x: (x[1], x[0].get("published") or ""), reverse=True)
    # Drop the weak tail: with a broad industry corpus almost every item matches
    # *something*, and a brief diluted with tangential stories is worse than a
    # shorter one. Keep items at least 40% as relevant as the best match.
    if scored:
        floor = max(3.0, scored[0][1] * 0.35)
        scored = [(it, s) for it, s in scored if s >= floor]

    # De-dup by normalized title, keeping the highest-scoring copy.
    seen: set[str] = set()
    kept: list[tuple[dict, float]] = []
    for it, s in scored:
        key = _norm_title(it.get("title") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        kept.append((it, s))
        if len(kept) >= limit:
            break

    if not kept:
        raise NoMarketDataError(
            ticker, ticker,
            f"ima 知识库中没有与 {ticker} 相关的内容（已同步 {len(items)} 条，但无命中）",
        )

    # 相关性硬门槛。只按板块词表匹配会让"任何半导体新闻"都算得上相关——
    # 000338（潍柴/重卡）会被塞进 7 条光刻胶、3D DRAM 的稿子，分析师只能
    # 自己声明"与本标的无关"，白耗上下文。这里要求命中该标的的别名（公司名/
    # 产品/产业链关键词）；一条都不命中时，最多给 3 条宏观背景，并明确说明
    # "库里没有该产业链的直接语料"，让分析师据此判断数据缺口而不是被误导。
    direct = [(it, sc) for it, sc in kept if _alias_hits(it, aliases) > 0]
    industry = [
        (it, sc) for it, sc in kept
        if _alias_hits(it, aliases) == 0 and _industry_hits(it, ticker) > 0
    ][:6]
    macro_only = [
        (it, sc) for it, sc in kept
        if _alias_hits(it, aliases) == 0 and _industry_hits(it, ticker) == 0
        and _bucket(it, aliases) == "宏观与政策"
    ][:3]

    if not direct and not industry and not macro_only:
        return (
            f"# ima 知识库情报 brief — {ticker}\n\n"
            f"**无相关语料**：库内已同步 {len(items)} 条，但没有一条涉及 {ticker} "
            f"或其产业链（{('、'.join(aliases[:4]) or '未配置别名')}）。\n\n"
            "这**不代表**该标的不存在消息，只说明 ima 里还没有相关内容。\n"
            "请仅依据 get_news / 公告 / 研报等工具下结论，并把此项记为数据缺口；"
            "若要补齐，可把该产业链的公众号文章推送进 ima 后重新同步。"
        )

    kept = direct + industry + macro_only
    industry_ids = {id(it) for it, _ in industry}

    # focus="industry"：市场分析师要的是"产业整体"，不是公司公告。只留行业与周期、
    # 上下游，以及研报类来源（行业展望/产能与价格跟踪/季度策略）——这些通常不点名
    # 个股，但恰恰决定"这波涨跌是景气驱动还是个股故事"。
    if focus == "industry":
        pool = [
            (it, sc) for it, sc in kept
            if _bucket(it, aliases) in ("行业与周期", "上下游与供应链")
            or "研报" in (it.get("source") or "")
        ]
        if not pool:
            return (
                f"# ima 产业情报 — {ticker}\n\n"
                f"**无产业级语料**：库内已同步 {len(items)} 条，但没有一条属于行业与周期、"
                "上下游或研报类内容。请以行情/技术面为主，并明确记为数据缺口；"
                "把行业研报、月度跟踪、涨价/产能类文章推进 ima 后即可补齐。"
            )
        kept = pool[:limit]
        direct = [(it, sc) for it, sc in kept if _alias_hits(it, aliases) > 0]
        industry = [(it, sc) for it, sc in kept if _alias_hits(it, aliases) == 0]
        macro_only = []
        industry_ids = {id(it) for it, _ in industry}

    buckets: dict[str, list[dict]] = {}
    for it, _ in kept:
        buckets.setdefault(_bucket(it, aliases), []).append(it)

    lines = [f"# ima 知识库情报 brief — {ticker}", ""]
    lines.append(
        f"来源：用户 ima 个人知识库（财联社/芯榜/芯智讯/半导体行业观察/梓豪谈芯/36氪/研报）。"
        f"已同步 {len(items)} 条，本次命中 {len(kept)} 条"
        f"（公司直接相关 {len(direct)} 条 / 行业与上下游 {len(industry)} 条 / 宏观背景 {len(macro_only)} 条，"
        f"已按标题去重）。"
    )
    if as_of_date:
        lines.append(f"时点约束：只使用 {as_of_date} 及之前发布的内容。")
    if not direct and industry:
        lines.append(
            "> 库内没有点名该公司的语料，但有**同产业链**内容（已标注〔行业/上下游相关〕）。"
            "可据此判断供给、需求、价格与产能方向，但不要把行业结论直接当成公司层面的确认。"
        )
    elif not direct and not industry:
        lines.append(
            "> ⚠️ 库内既没有点名该公司的语料，也没有其产业链内容，下面只是宏观背景。"
            "请把行业细节记为数据缺口，不要外推。"
        )
    lines.append("")

    for section in ("公司自身", "上下游与供应链", "行业与周期", "宏观与政策", "其他"):
        group = buckets.get(section)
        if not group:
            continue
        lines.append(f"## {section}（{len(group)} 条）")
        for it in group[:_MAX_PER_SECTION]:
            tag = ("〔行业/上下游相关，未点名公司〕"
                   if id(it) in industry_ids and section != "宏观与政策" else "")
            lines.append(_fmt_item(it, tag))
        lines.append("")

    # Explicit trend read: the user asked to watch for change, not levels.
    trend_hits = []
    for it, _ in kept:
        flags = _trend_flags((it.get("title") or "") + " " + (it.get("text") or "")[:400])
        if flags:
            trend_hits.append(f"- [{(it.get('published') or '')[:10]}] "
                              f"{(it.get('title') or '')[:60]} → {'/'.join(flags[:4])}")
    if trend_hits:
        lines.append("## 趋势变化线索（按时间由新到旧）")
        lines.extend(trend_hits[:8])
        lines.append("")

    # Feedback loop: what we predicted before, and whether it held.
    lines.extend(_render_ledger(ticker, as_of_date))
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# Prediction ledger: 反馈 → 验证 → 沉淀 → 再预测
# ---------------------------------------------------------------------------
def load_ledger(ticker: str | None = None) -> list[dict]:
    path = ledger_path()
    if not os.path.exists(path):
        return []
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ticker is None or rec.get("ticker") == ticker:
                    out.append(rec)
    except OSError:
        return []
    return out


def _render_ledger(ticker: str, as_of_date: str | None) -> list[str]:
    """Scorecard of earlier predictions: verified hit/miss, then open ones."""
    entries = [e for e in load_ledger(ticker)
               if not as_of_date or (e.get("as_of") or "") <= as_of_date]
    if not entries:
        return [
            "## 复盘（历史预测验证）",
            "- 暂无历史预测记录。本次判断将成为第一条，后续会自动校验并被继承。",
            "",
        ]
    verified = [e for e in entries if e.get("hit") is not None]
    open_ = [e for e in entries if e.get("hit") is None]
    lines = ["## 复盘（历史预测验证）"]
    if verified:
        hits = sum(1 for e in verified if e.get("hit"))
        lines.append(
            f"- 已验证 {len(verified)} 条，命中 {hits} 条，命中率 "
            f"{hits / len(verified) * 100:.0f}%。"
        )
        for e in verified[-5:]:
            mark = "✓" if e.get("hit") else "✗"
            realized = e.get("realized_pct")
            realized_txt = f"{realized:+.1f}%" if isinstance(realized, (int, float)) else "n/a"
            lines.append(
                f"  - {mark} [{e.get('as_of')}] {e.get('claim', '')[:70]}"
                f" → 实际 {realized_txt}（{e.get('horizon_days')}日）"
            )
    for e in open_[-3:]:
        lines.append(
            f"  - ⏳ [{e.get('as_of')}] {e.get('claim', '')[:70]}"
            f"（{e.get('horizon_days')}日窗口，待验证）"
        )
    lines.append("")
    lines.append(
        "**用法**：先判断上面被证伪的条目错在哪（假设错了 / 时点错了 / 只是噪声），"
        "再决定本次是否沿用同一逻辑；被验证有效的驱动因素应加权，被证伪的应降权或剔除。"
    )
    lines.append("")
    return lines
