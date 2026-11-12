"""本地知识库检索（IMA 资料落地后的本地检索）。

用户把 IMA 知识库里的研报/公众号文章/笔记导出成 markdown 文件，放在
``data/knowledge/`` 目录下（子目录按类型划分：reports/ news/ notes/）。
本模块在分析某只股票时，按该公司名 + 行业关键词检索这些文件，把命中的
内容块返回给新闻/情绪分析师，作为「专属信息源」注入。

与 router 的 VENDOR_METHODS 链不同，这是一个独立的检索模块（类似
wechat / stocktwits / sentisense 的独立函数），由分析师直接调用，不走
vendor 链，也不需要 API key。
"""

from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

# knowledge 目录：优先环境变量，其次相对项目根的 data/knowledge
# 兼容两种情况：
#   1. 从项目根运行（data/knowledge）
#   2. 通过 TRADINGAGENTS_KNOWLEDGE_DIR 显式指定
_KNOWLEDGE_DIR_ENV = "TRADINGAGENTS_KNOWLEDGE_DIR"


def get_knowledge_dir() -> str:
    """返回知识库目录路径（确保存在）。"""
    env = os.getenv(_KNOWLEDGE_DIR_ENV)
    if env:
        return env
    # 相对项目根：当前文件在 <root>/tradingagents/dataflows/vendors/ 下，往上 3 层到项目根
    here = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(here, "..", "..", ".."))
    return os.path.join(project_root, "data", "knowledge")


# 行业关键词映射：当股票属于某行业时，额外用这些行业词检索
_INDUSTRY_KEYWORDS = [
    "半导体", "存储", "DRAM", "HBM", "NAND", "芯片", "晶圆", "先进制程",
    "光刻", "刻蚀", "封装", "台积电", "海力士", "美光", "三星",
    "AI", "人工智能", "算力", "GPU", "ASIC", "CoWoS",
]

# ticker → 公司中文名 映射（用于检索时带上公司名做精确匹配）
# 覆盖用户分析过的半导体产业链标的；未收录的标的使用行业关键词兜底。
_COMPANY_NAME_CN = {
    # A 股
    "603259": "药明康德",
    "688981": "中芯国际",
    "002371": "北方华创",
    "002475": "立讯精密",
    "300308": "中际旭创",
    "688008": "澜起科技",
    "688102": "斯瑞新材",
    "688111": "金山办公",
    # 港股
    "0700.HK": "腾讯控股",
    "9988.HK": "阿里巴巴",
    "3690.HK": "美团",
    "1810.HK": "小米集团",
    # 美股
    "NVDA": "英伟达",
    "AMD": "超威半导体",
    "INTC": "英特尔",
    "MU": "美光科技",
    "SKHY": "海力士",
    "SNDK": "闪迪",
    "WDC": "西部数据",
    "TSM": "台积电",
    "AVGO": "博通",
    "MRVL": "迈威尔",
    "QCOM": "高通",
    "ARM": "安谋",
    "GLW": "康宁",
    "LITE": "Lumentum",
    "COHR": "Coherent",
    "AAOI": "应用光电",
    "ASML": "阿斯麦",
    "AMAT": "应用材料",
    "LRCX": "泛林",
    "KLAC": "科磊",
    "SIMO": "慧荣科技",
}


def get_company_name_cn(ticker: str) -> str:
    """返回 ticker 的公司中文名（仅覆盖映射表内标的，否则返回空串）。"""
    return _COMPANY_NAME_CN.get((ticker or "").upper(), "")


def _iter_md_files(knowledge_dir: str) -> list[str]:
    """列出 knowledge 目录下所有 .md 文件（含子目录）。"""
    files = []
    if not os.path.isdir(knowledge_dir):
        return files
    for root, _dirs, names in os.walk(knowledge_dir):
        for name in names:
            if name.endswith(".md") and name.upper() != "README.MD":
                files.append(os.path.join(root, name))
    return files


def _tokenize(text: str) -> list[str]:
    """粗粒度分词：中文按标点/空格切分，英文按非字母数字切分。"""
    # 去掉 markdown 语法和标点
    cleaned = re.sub(r"[#*_`>|\-\[\]()（）【】《》、，。；：！？\s]+", " ", text)
    return [t for t in cleaned.split(" ") if len(t) >= 2]


def _keyword_hit_score(content: str, keywords: list[str]) -> int:
    """计算内容对关键词的命中得分（命中次数加权）。

    公司名等「专属关键词」权重远高于通用行业词，保证检索结果优先返回
    真正与该标的相关的内容，而不是所有半导体标的都命中同一批通用文件。
    """
    if not keywords:
        return 0
    score = 0
    lower = content.lower()
    head = lower[:200]  # 标题区
    for kw in keywords:
        kw_l = kw.lower()
        cnt = lower.count(kw_l)
        if not cnt:
            continue
        weight = 2 if kw_l in head else 1
        score += cnt * weight
    return score


def _company_hit_score(content: str, company_keywords: list[str]) -> int:
    """计算内容对「公司专属关键词」的命中得分（高权重，用于区分标的）。

    公司名命中是强信号，权重远高于行业词，避免通用文件霸占检索结果。
    """
    if not company_keywords:
        return 0
    score = 0
    lower = content.lower()
    head = lower[:200]
    for kw in company_keywords:
        kw_l = kw.lower()
        if kw_l in head:
            score += 100  # 标题命中：极强信号
        score += lower.count(kw_l) * 20  # 正文命中：强信号
    return score


def fetch_local_knowledge(ticker: str, max_chars: int = 4000) -> str:
    """高层入口：按 ticker 检索本地知识库，自动带公司中文名。

    分析师直接调用这个函数即可，无需关心公司名映射细节。返回命中的
    资料内容（markdown），无命中返回空串（调用方按空串跳过注入）。
    """
    name = get_company_name_cn(ticker)
    return search_knowledge(ticker, company_name=name, max_chars=max_chars)


def search_knowledge(
    ticker: str,
    company_name: str = "",
    max_chars: int = 4000,
) -> str:
    """检索本地知识库，返回与 ``ticker`` 相关的资料内容块。

    Args:
        ticker: 股票代码（如 "603259"、"0700.HK"、"NVDA"）
        company_name: 公司中文名（如 "药明康德"、"海力士"、"美光"），
            为空则仅用行业关键词 + 代码检索。
        max_chars: 返回内容的最大字符数（截断超长内容）。

    Returns:
        str: 命中的资料内容（markdown 格式），无命中则返回空字符串。
    """
    knowledge_dir = get_knowledge_dir()
    if not os.path.isdir(knowledge_dir):
        return ""

    # 构建关键词：公司名（专属，高权重）+ 行业关键词（通用，低权重）
    company_keywords = []
    if company_name:
        company_keywords.append(company_name)
        for part in re.split(r"[\s/、]+", company_name):
            if part and part not in company_keywords:
                company_keywords.append(part)
    # 行业关键词：去重
    industry_keywords = list(dict.fromkeys(_INDUSTRY_KEYWORDS))

    # 收集命中的文件及其得分：公司名命中权重远高于行业词
    hits = []  # (score, path)
    files = _iter_md_files(knowledge_dir)
    for path in files:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except Exception:
            continue
        company_score = _company_hit_score(content, company_keywords)
        industry_score = _keyword_hit_score(content, industry_keywords)
        # 关键：公司名命中是「强信号」，行业词命中仅作排序微调。
        # 若提供了公司名，则只返回真正提到该公司的文件（避免把无关标的
        # 也注入一堆半导体行业通用内容）；未提供公司名时才退回行业词兜底。
        if company_keywords and company_score == 0:
            continue
        # 公司名命中是主排序依据；行业词只做极小的次级权重，
        # 避免通用行业词（如"半导体"在 HBM 文章里出现几百次）淹没
        # 公司名的区分度，导致所有标的都返回同一批通用文件。
        score = company_score * 100 + industry_score
        if score > 0:
            hits.append((score, path))

    if not hits:
        return ""

    # 按得分降序，取前若干份，拼成 markdown 块
    hits.sort(key=lambda x: -x[0])
    parts = []
    total = 0
    for score, path in hits[:5]:  # 最多取 5 份资料
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except Exception:
            continue
        # 文件名作为小节标题
        title = os.path.splitext(os.path.basename(path))[0]
        rel = os.path.relpath(path, knowledge_dir)
        parts.append(f"### 本地知识库：{title}")
        parts.append(f"（来源：{rel}，命中度 {score}）")
        parts.append("")
        # 截断超长内容
        if len(content) > max_chars:
            content = content[:max_chars] + "\n...(内容过长，已截断)"
        parts.append(content)
        parts.append("")
        total += len(content)
        if total > max_chars * 2:  # 总量上限，避免注入过长
            break

    return "\n".join(parts).rstrip()


def get_knowledge_summary() -> str:
    """返回知识库概览（有哪些资料），用于诊断/展示。"""
    knowledge_dir = get_knowledge_dir()
    files = _iter_md_files(knowledge_dir)
    if not files:
        return "本地知识库为空"
    lines = [f"本地知识库共 {len(files)} 份资料："]
    for path in files:
        rel = os.path.relpath(path, knowledge_dir)
        size = os.path.getsize(path)
        lines.append(f"  - {rel} ({size} 字节)")
    return "\n".join(lines)


if __name__ == "__main__":
    # 简单自测
    print(get_knowledge_summary())
    print("\n--- 检索 '海力士 存储' ---")
    print(search_knowledge("SKHY", "海力士", max_chars=800)[:800])
