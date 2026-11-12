# 本地知识库（IMA 资料落地）

本目录存放从用户 IMA 知识库「Grit Steadfast的知识库」导出的投资相关资料，
作为 TradingAgents 分析时的**专属信息源**，由 `local_knowledge` vendor 检索注入。

## 目录结构

```
data/knowledge/
├── reports/   研报类（券商研报、投资建议、评分报告等）
├── news/      资讯/公众号文章（行业观察、公司动态）
└── notes/     个人笔记/观点（预留）
```

## 当前资料清单（8 份）

### 研报类（reports/）
1. `2026全球半导体展望.md` — 台积电产能、CoWoS、DRAM/HBM/NAND 价格、中国先进制程产能
2. `Semiconductor-Expansion-2026-H2.md` — 2026 下半年半导体扩产展望
3. `2026Q4投资建议-美股.md` — 92 只 AI/半导体美股估值评分表
4. `2026H2投资建议-AH股.md` — 60+ 只 A+H 半导体标的推荐指数与说明
5. `头部梯队券商26Q3业绩披露.md` — 头部券商 26Q3 业绩

### 资讯/公众号（news/）
6. `存储的天塌了-台积电评估3D_DRAM的真正影响.md` — 梓豪谈芯
7. `HBM最终被取代-台积电评估3D_DRAM的后果很严重.md` — 梓豪谈芯
8. `高通第一代HBC吊打HBM.md` — 半导体行业观察

## 如何接入分析流程

- `local_knowledge` vendor（`tradingagents/dataflows/vendors/local_knowledge.py`）
  提供 `fetch_local_knowledge(ticker)`，按「公司中文名 + 行业关键词」检索本目录，
  命中的内容注入 **新闻分析师** 和 **情绪分析师** 的 system prompt。
- 公司名映射维护在模块内的 `TICKER_NAME_CN` 字典里，新增标的需要补充映射。

## 如何同步新增的 IMA 资料

> 关键限制：IMA 是 WorkBuddy 宿主能力，本地脚本无法直接调用 ima 工具。
> 新增资料需要**在 WorkBuddy 对话中由 AI 完成导出**。

**步骤：**
1. 在 IMA 里新增/更新资料
2. 回到 WorkBuddy 对话，说一句类似：
   > 「我 IMA 里新增了资料，帮我重新导出到 TradingAgents 本地知识库」
3. AI 会用 ima 工具读取新资料，清理后写入本目录对应子目录（reports/ 或 news/）
4. 若新增的是之前没有的标的，同时提醒 AI 在 `local_knowledge.py` 的
   `TICKER_NAME_CN` 里补上对应的公司中文名映射

## 检索原理

`fetch_local_knowledge(ticker)` 工作流：
1. `get_company_name_cn(ticker)` → 取公司中文名（如 NVDA → 英伟达）
2. 用公司名 + 半导体行业关键词在全部 md 文件里做文本匹配
3. 公司名命中是**强信号**（未命中该公司名则返回空，避免注入无关内容）
4. 按「公司名命中次数为主、行业词为辅」排序，取前若干份命中内容注入
