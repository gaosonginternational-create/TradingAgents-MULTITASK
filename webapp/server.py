#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TradingAgents 本地工作台后端服务
零第三方依赖（仅 Python 标准库），提供：
  - 报告浏览 / 读取 / Markdown 渲染
  - 发起新分析（调用 docker compose）
  - 实时日志推送（Server-Sent Events）
  - 任务管理
"""
import json
import os
import re
import subprocess
import threading
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS_DIR = os.path.join(BASE_DIR, "data", "logs", "reports")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
TASKS_FILE = os.path.join(BASE_DIR, "data", "logs", "tasks.json")
COMPANY_NAMES_FILE = os.path.join(BASE_DIR, "data", "logs", "company_names.json")
SCHEDULES_FILE = os.path.join(BASE_DIR, "data", "logs", "schedules.json")
PORT = 8765

# 定时任务默认触发时间（每天 23:00 后开始；实际是"当天 23:00 起、下次运行前"这个窗口内触发一次）
SCHEDULE_HOUR = 23
SCHEDULE_MINUTE = 0

# ANSI 转义序列（颜色码、光标控制）与 tqdm 进度条控制字符，清理后写入日志/SSE，
# 避免前端日志控制台显示乱码。
_ANSI_ESCAPE = re.compile(
    r"\x1b(?:\[[0-9;?]*[a-zA-Z]|\][^\x07\x1b]*(?:\x07|\x1b\\)|\([A-Za-z0-9])"
)
# 单独的 \r 被 tqdm 用来原地刷新进度条；前端按行渲染会看到残片，统一替换为换行。
_CTRL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_log_line(line: str) -> str:
    """去除日志行中的 ANSI 转义码与非打印控制字符，返回干净文本。"""
    if not line:
        return line
    line = _ANSI_ESCAPE.sub("", line)
    line = _CTRL_CHARS.sub("", line)
    return line.rstrip("\r")

# 内置常见 A 股/ETF 代码 -> 公司简称（补充：从报告正文自动学习 + 手动扩充）
BUILTIN_COMPANY_NAMES = {
    # 宽基/热门 ETF
    "510300": "沪深300ETF",
    "510310": "沪深300ETF易方达",
    "510330": "华夏沪深300ETF",
    "510050": "上证50ETF",
    "510500": "中证500ETF",
    "512100": "中证1000ETF",
    "588000": "科创50ETF",
    "588170": "科创半导体ETF",
    "588200": "科创芯片ETF",
    "588080": "科创板50ETF",
    "159915": "创业板ETF",
    "159845": "中证1000ETF(深)",
    "159949": "创业板50ETF",
    "512480": "半导体ETF",
    "512690": "酒ETF",
    "512880": "证券ETF",
    "515790": "光伏ETF",
    # 热门 A 股
    "600519": "贵州茅台",
    "601318": "中国平安",
    "600036": "招商银行",
    "601899": "紫金矿业",
    "600900": "长江电力",
    "601398": "工商银行",
    "601288": "农业银行",
    "601988": "中国银行",
    "600030": "中信证券",
    "601012": "隆基绿能",
    "600276": "恒瑞医药",
    "600887": "伊利股份",
    "601166": "兴业银行",
    "600000": "浦发银行",
    "000001": "平安银行",
    "000002": "万科A",
    "000333": "美的集团",
    "000651": "格力电器",
    "000858": "五粮液",
    "002594": "比亚迪",
    "002415": "海康威视",
    "300750": "宁德时代",
    "300059": "东方财富",
    "300015": "爱尔眼科",
    "688981": "中芯国际",
    "688111": "金山办公",
    # 补充：半导体/科技热门标的
    "002371": "北方华创",
    "002475": "立讯精密",
    "300308": "中际旭创",
    "688008": "澜起科技",
    "688102": "斯瑞新材",
    "159852": "软件ETF嘉实",
}

# 港股 / 美股常见标的简称（代码 -> 中文名）
BUILTIN_GLOBAL_NAMES = {
    # 港股
    "0700.HK": "腾讯控股",
    "9988.HK": "阿里巴巴",
    "3690.HK": "美团",
    "9618.HK": "京东集团",
    "1810.HK": "小米集团",
    "9999.HK": "网易",
    "1024.HK": "快手",
    "1211.HK": "比亚迪股份",
    "0388.HK": "香港交易所",
    "2318.HK": "中国平安",
    "2628.HK": "中国人寿",
    "3968.HK": "招商银行",
    "0939.HK": "建设银行",
    "1398.HK": "工商银行",
    "3988.HK": "中国银行",
    "0005.HK": "汇丰控股",
    "1299.HK": "友邦保险",
    "2015.HK": "理想汽车",
    "9868.HK": "小鹏汽车",
    "9866.HK": "蔚来",
    "2800.HK": "盈富基金",
    "2828.HK": "恒生中国企业",
    "0700.HK": "腾讯控股",
    # 美股
    "AAPL": "苹果",
    "MSFT": "微软",
    "GOOGL": "谷歌",
    "GOOG": "谷歌",
    "AMZN": "亚马逊",
    "NVDA": "英伟达",
    "META": "Meta",
    "TSLA": "特斯拉",
    "BRK.B": "伯克希尔",
    "JPM": "摩根大通",
    "V": "Visa",
    "JNJ": "强生",
    "WMT": "沃尔玛",
    "PG": "宝洁",
    "MA": "万事达",
    "HD": "家得宝",
    "DIS": "迪士尼",
    "NFLX": "奈飞",
    "AMD": "超威半导体",
    "INTC": "英特尔",
    "BABA": "阿里巴巴",
    "PDD": "拼多多",
    "BIDU": "百度",
    "JD": "京东",
    "NIO": "蔚来",
    "LI": "理想汽车",
    "XPEV": "小鹏汽车",
    "COIN": "Coinbase",
    "SMCI": "超微电脑",
    "LITE": "Lumentum",
    "SKHY": "SK海力士",
    "GLW": "康宁",
    "MRVL": "迈威尔科技",
    "MU": "美光科技",
    "SNDK": "闪迪",
    "SOXX": "费城半导体ETF",
}

# 分析师 key -> 中文名
ANALYST_NAMES = {
    "market": "市场分析师",
    "sentiment": "情绪分析师",
    "news": "新闻分析师",
    "fundamentals": "基本面分析师",
    "bull": "多头研究员",
    "bear": "空头研究员",
    "manager": "研究经理",
    "trader": "交易员",
    "aggressive": "激进风控",
    "conservative": "保守风控",
    "neutral": "中性风控",
    "decision": "投资组合经理",
}

# 运行中的任务
RUNNING_TASKS = {}          # task_id -> {proc, log_lines, status, ...}
TASKS_LOCK = threading.Lock()

# ---- 公司简称缓存（A 股/ETF 代码 -> 简称）----
COMPANY_NAMES_LOCK = threading.Lock()


def _load_company_names():
    """加载公司简称缓存（内置表 + 磁盘缓存）"""
    names = dict(BUILTIN_COMPANY_NAMES)
    if os.path.exists(COMPANY_NAMES_FILE):
        try:
            with open(COMPANY_NAMES_FILE, "r", encoding="utf-8") as f:
                extra = json.load(f)
            if isinstance(extra, dict):
                names.update(extra)
        except Exception:
            pass
    return names


def _save_company_names(names):
    try:
        os.makedirs(os.path.dirname(COMPANY_NAMES_FILE), exist_ok=True)
        tmp = COMPANY_NAMES_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(names, f, ensure_ascii=False)
        os.replace(tmp, COMPANY_NAMES_FILE)
    except Exception:
        pass


COMPANY_NAMES = _load_company_names()


def _learn_company_name_from_report(report_dir: str) -> dict:
    """从报告正文学习 A 股代码的公司简称，返回 {code: name}。

    报告正文里分析师常写「600519（贵州茅台）」或「贵州茅台（600519）」，
    从市场/基本面分析师报告的前若干行提取，命中则缓存。

    为避免把标题（如「公司新闻与事件梳理（603259）」）误判为公司名，
    只信任以下两类「高置信」写法：
      1. 行首标题「# 603259（药明康德…）」——代码在行首、紧跟括号的公司名；
      2. 「药明康德（603259）」——纯中文（2~6 字，不含标题惯用词）后紧跟括号代码。
    """
    # 标题惯用词：命中这些词说明括号前是章节标题而非公司名
    _SECTION_WORDS = ("梳理", "分析", "研究", "报告", "新闻", "事件", "观点", "综述", "总结", "概览")
    learned = {}
    for folder in ("1_analysts",):
        base = os.path.join(report_dir, folder)
        if not os.path.isdir(base):
            continue
        for fn in os.listdir(base):
            if not fn.endswith(".md"):
                continue
            path = os.path.join(base, fn)
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    text = f.read(4000)
            except Exception:
                continue

            # 规则1：标题行「# 603259（药明康德 / WuXi AppTec）…」——取括号内第一个中文/字母段
            # 允许括号内含「药明康德 / WuXi AppTec」这类带空格斜杠的写法，截取首个片段
            for m in re.finditer(r"^#+\s*(\d{6})\s*[（(]\s*([\u4e00-\u9fa5A-Za-z]{2,12})", text, flags=re.MULTILINE):
                code, name = m.group(1), m.group(2).strip()
                if name and not re.fullmatch(r"[A-Za-z]+", name):  # 排除纯英文（不是中文简称）
                    learned[code] = name

            # 规则2：「药明康德（603259）」——纯中文 2~6 字，且不含标题惯用词
            for m in re.finditer(r"([\u4e00-\u9fa5]{2,6})\s*[（(]\s*(\d{6})\s*[)）]", text):
                name, code = m.group(1).strip(), m.group(2)
                if name and not any(w in name for w in _SECTION_WORDS):
                    learned[code] = name
    return learned


def get_company_name(ticker: str) -> str:
    """返回 ticker 的公司简称。

    A 股/ETF（6位数字）查缓存表；港股/美股查内置常见名称表；其余返回空串。
    """
    t = (ticker or "").strip()
    if not t:
        return ""
    if re.fullmatch(r"\d{6}", t):
        with COMPANY_NAMES_LOCK:
            return COMPANY_NAMES.get(t, "")
    # 港股/美股：统一大写匹配内置表（港股代码如 0700.HK，美股如 AAPL）
    return BUILTIN_GLOBAL_NAMES.get(t.upper(), "")


# ---- 任务持久化：服务重启后任务记录不丢失 ----
def _load_tasks_history():
    """从磁盘加载历史任务（仅保留轻量元数据，不含 log_lines/proc）。

    仅在 tasks.json 文件不存在（首次启动）时才从报告目录重建；
    文件存在但为空（用户主动清空过）则尊重用户意图，不重建。
    """
    if os.path.exists(TASKS_FILE):
        try:
            with open(TASKS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        # 文件存在但损坏：返回空，不重建（避免反复从报告恢复已删除的任务）
        return {}
    # tasks.json 不存在：从报告目录重建历史任务（每个报告视为一条已完成任务）
    rebuilt = _rebuild_tasks_from_reports()
    if rebuilt:
        _write_tasks_file(rebuilt)
    return rebuilt


def _rebuild_tasks_from_reports():
    """从 reports 目录重建任务历史，返回 {task_id: meta} 字典"""
    rebuilt = {}
    if not os.path.isdir(REPORTS_DIR):
        return rebuilt
    for name in os.listdir(REPORTS_DIR):
        d = os.path.join(REPORTS_DIR, name)
        if not os.path.isdir(d):
            continue
        # 目录名形如 TICKER_YYYYMMDD_HHMMSS
        m = re.match(r"^([A-Za-z0-9.\-]+)_(\d{8})_(\d{6})$", name)
        if not m:
            continue
        ticker = m.group(1)
        # 用目录名里的时间戳生成稳定的 task_id（毫秒级），避免与内存任务冲突
        try:
            import datetime as _dt
            dt = _dt.datetime.strptime(m.group(2) + m.group(3), "%Y%m%d%H%M%S")
            ts = dt.timestamp()
        except Exception:
            ts = os.path.getmtime(d)
        # 检查报告是否完整（有 md 或 html 才算完成）
        has_md = os.path.exists(os.path.join(d, "complete_report.md"))
        has_html = os.path.exists(os.path.join(d, "complete_report.html"))
        if not (has_md or has_html):
            continue
        task_id = str(int(ts * 1000))
        rebuilt[task_id] = {
            "ticker": ticker,
            "date": f"{m.group(2)[:4]}-{m.group(2)[4:6]}-{m.group(2)[6:8]}",
            "analysts": "market,sentiment,news,fundamentals",
            "status": "done",
            "exit_code": 0,
            "created_at": ts,
            "finished_at": ts,
        }
    return rebuilt


def _write_tasks_file(data):
    """把任务元数据字典写入磁盘"""
    try:
        os.makedirs(os.path.dirname(TASKS_FILE), exist_ok=True)
        tmp = TASKS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, TASKS_FILE)
    except Exception:
        pass


def _save_tasks_history():
    """把任务元数据落盘（只存轻量字段，剔除 proc/线程对象）"""
    with TASKS_LOCK:
        persist = {}
        for tid, t in RUNNING_TASKS.items():
            persist[tid] = {
                "ticker": t.get("ticker"),
                "date": t.get("date"),
                "analysts": t.get("analysts"),
                "status": t.get("status"),
                "exit_code": t.get("exit_code"),
                "created_at": t.get("created_at"),
                "finished_at": t.get("finished_at"),
            }
    # 同时把历史任务也一并落盘，保证重启后完整
    merged = dict(TASKS_HISTORY)
    merged.update(persist)
    _write_tasks_file(merged)


TASKS_HISTORY = _load_tasks_history()  # task_id -> 轻量元数据（重启后仍可读）


# ================= 定时任务 =================
SCHEDULES_LOCK = threading.Lock()


def _load_schedules():
    """从磁盘加载定时任务配置。返回 {ticker: {enabled, hour, minute, analysts}}。"""
    if os.path.exists(SCHEDULES_FILE):
        try:
            with open(SCHEDULES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def _write_schedules(data):
    try:
        os.makedirs(os.path.dirname(SCHEDULES_FILE), exist_ok=True)
        tmp = SCHEDULES_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SCHEDULES_FILE)
    except Exception:
        pass


SCHEDULES = _load_schedules()

# 记录每个 ticker 上一次触发调度的日期（YYYY-MM-DD），防止同一天重复触发
_last_schedule_trigger = {}


def _should_trigger(ticker: str, cfg: dict) -> bool:
    """判断某 ticker 的定时任务此刻是否该触发。"""
    if not cfg.get("enabled", False):
        return False
    hour = int(cfg.get("hour", SCHEDULE_HOUR))
    minute = int(cfg.get("minute", SCHEDULE_MINUTE))
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    # 已过设定的触发时刻（HH:MM），且今天尚未触发过
    if (now.hour, now.minute) >= (hour, minute):
        if _last_schedule_trigger.get(ticker) != today:
            return True
    return False


def _trigger_scheduled_analysis(ticker: str, cfg: dict):
    """触发一次定时分析，复用 /api/analyze 的底层 run_analysis。"""
    analysts = cfg.get("analysts", "market,sentiment,news,fundamentals")
    date = datetime.now().strftime("%Y-%m-%d")
    task_id = make_task_id()
    with TASKS_LOCK:
        RUNNING_TASKS[task_id] = {
            "ticker": ticker,
            "date": date,
            "analysts": analysts,
            "status": "starting",
            "log_lines": [f"[定时任务] 自动触发 {ticker} 分析（{date}）"],
            "exit_code": None,
            "created_at": time.time(),
            "finished_at": None,
            "scheduled": True,
        }
    _save_tasks_history()
    _last_schedule_trigger[ticker] = date
    t = threading.Thread(target=run_analysis, args=(ticker, date, analysts, task_id), daemon=True)
    t.start()


def _scheduler_loop():
    """后台调度线程：每分钟检查一次，触发到点的定时任务。"""
    while True:
        try:
            with SCHEDULES_LOCK:
                items = list(SCHEDULES.items())
            for ticker, cfg in items:
                if _should_trigger(ticker, cfg):
                    _trigger_scheduled_analysis(ticker, cfg)
        except Exception:
            pass
        time.sleep(60)  # 每分钟检查一次


def list_reports():
    """列出所有历史报告（按时间倒序）"""
    items = []
    if not os.path.isdir(REPORTS_DIR):
        return items
    for name in os.listdir(REPORTS_DIR):
        d = os.path.join(REPORTS_DIR, name)
        if not os.path.isdir(d):
            continue
        md = os.path.join(d, "complete_report.md")
        html = os.path.join(d, "complete_report.html")
        if not (os.path.exists(md) or os.path.exists(html)):
            continue
        # 从目录名解析 ticker 和日期，形如 SKHY_20261109_160242
        m = re.match(r"^([A-Za-z0-9.\-]+)_(\d{8})_(\d{6})$", name)
        ticker = m.group(1) if m else name
        date_str = ""
        if m:
            date_str = f"{m.group(2)[:4]}-{m.group(2)[4:6]}-{m.group(2)[6:8]} {m.group(3)[:2]}:{m.group(3)[2:4]}:{m.group(3)[4:6]}"
        # 读取评级
        rating = ""
        if os.path.exists(md):
            try:
                with open(md, "r", encoding="utf-8", errors="ignore") as f:
                    head = f.read(500)
                rm = re.search(r"Rating:\s*([A-Za-z]+)", head)
                if rm:
                    rating = rm.group(1)
            except Exception:
                pass
        # 公司简称：A 股/ETF 查缓存；缓存未命中则从报告正文学习（仅首次，之后命中缓存）
        cn_name = get_company_name(ticker)
        if not cn_name and re.fullmatch(r"\d{6}", ticker):
            learned = _learn_company_name_from_report(d)
            if learned:
                with COMPANY_NAMES_LOCK:
                    for c, nm in learned.items():
                        COMPANY_NAMES.setdefault(c, nm)
                _save_company_names(dict(COMPANY_NAMES))
                cn_name = learned.get(ticker, "")
        items.append({
            "id": name,
            "ticker": ticker,
            "name": cn_name,
            "date": date_str,
            "rating": rating,
            "mtime": int(os.path.getmtime(d)),
            "has_md": os.path.exists(md),
            "has_html": os.path.exists(html),
        })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


def classify_market(ticker: str) -> str:
    """根据 ticker 判断市场类别，返回：A股 / ETF / 港股 / 美股 / 其它。"""
    t = (ticker or "").upper()
    # 港股：形如 0700.HK / 9988.HK
    if re.fullmatch(r"\d{1,5}\.HK", t):
        return "港股"
    # 6 位纯数字：先判 ETF，再判 A股
    if re.fullmatch(r"\d{6}", t):
        p2 = t[:2]
        etf_sh = {"51", "52", "53", "56", "58", "59"}
        etf_sz = {"15", "16", "18"}
        if p2 in etf_sh or p2 in etf_sz:
            return "ETF"
        return "A股"
    # 美股：英文字母代码（含 .US / .O 等后缀）
    if re.fullmatch(r"[A-Z]{1,5}(\.[A-Z]{1,3})?", t):
        return "美股"
    return "其它"


def list_report_groups():
    """把历史报告按标的（ticker）聚合为文件夹结构。

    返回 [{ticker, name, market, count, latest_rating, reports: [平铺 item...]}]
    reports 内部仍沿用 list_reports 的 item 结构，前端可复用渲染逻辑。
    """
    flat = list_reports()
    groups = {}
    for item in flat:
        key = item["ticker"].upper()
        g = groups.get(key)
        if g is None:
            g = {
                "ticker": item["ticker"],
                "name": item["name"],
                "market": classify_market(item["ticker"]),
                "count": 0,
                "latest_rating": item["rating"],
                "reports": [],
            }
            groups[key] = g
        g["count"] += 1
        # 首次（最新，因为 list_reports 已按 mtime 倒序）即为最新评级
        if not g["latest_rating"] and item["rating"]:
            g["latest_rating"] = item["rating"]
        g["reports"].append(item)
    # 按最近报告时间倒序排列文件夹
    result = list(groups.values())
    result.sort(key=lambda g: g["reports"][0]["mtime"], reverse=True)
    return result


def read_text(path, limit=None):
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        if limit:
            return f.read(limit)
        return f.read()


def resolve_report_path(report_id, rel):
    """安全解析报告内文件路径，防止目录穿越"""
    base = os.path.realpath(os.path.join(REPORTS_DIR, report_id))
    if not base.startswith(os.path.realpath(REPORTS_DIR)):
        return None
    target = os.path.realpath(os.path.join(base, rel))
    if not target.startswith(base):
        return None
    return target if os.path.exists(target) else None


def list_report_stages(report_id):
    """列出单个报告的各阶段文件树"""
    base = os.path.join(REPORTS_DIR, report_id)
    stages = []
    if not os.path.isdir(base):
        return stages
    stage_order = {
        "1_analysts": "分析师团队",
        "2_research": "牛熊研究辩论",
        "3_trading": "交易员",
        "4_risk": "风控三派",
        "5_portfolio": "投资组合经理",
    }
    for d in sorted(os.listdir(base)):
        full = os.path.join(base, d)
        if not os.path.isdir(full):
            continue
        files = []
        for fn in sorted(os.listdir(full)):
            if fn.endswith(".md"):
                key = fn[:-3]
                files.append({"key": key, "name": ANALYST_NAMES.get(key, key), "file": fn})
        stages.append({
            "dir": d,
            "label": stage_order.get(d, d),
            "files": files,
        })
    return stages


# 分析超时上限（秒）。4 分析师全开时约 5~8 分钟，
# 这里设 30 分钟上限，超时自动终止进程，避免前端永远"运行中"。
ANALYSIS_TIMEOUT = 30 * 60


def run_analysis(ticker, date, analysts, task_id):
    """在子线程中运行分析，实时收集日志"""
    with TASKS_LOCK:
        task = RUNNING_TASKS[task_id]
    # 注意：不加 --show（改为 --no-show），否则在无 TTY 环境下 Rich 分页器会挂起，
    # 导致容器永不退出、任务卡在 running。报告已通过 --save 保存到文件，前端直接读文件展示。
    cmd = ["docker", "compose", "run", "--rm", "tradingagents",
           "--ticker", ticker, "--date", date,
           "--analysts", analysts, "--save", "--html", "--no-show"]
    proc = subprocess.Popen(
        cmd, cwd=BASE_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="ignore", bufsize=1,
        shell=False,
    )
    task["proc"] = proc
    task["status"] = "running"
    started = time.time()
    timed_out = False
    try:
        # 逐行读取 + 超时轮询：proc.stdout 是阻塞的，用单独线程读，
        # 主循环负责超时判断，超时则 kill 整个 docker compose 进程树。
        def _reader():
            for line in proc.stdout:
                with TASKS_LOCK:
                    task["log_lines"].append(clean_log_line(line))
                    if len(task["log_lines"]) > 500:
                        task["log_lines"] = task["log_lines"][-500:]
        reader = threading.Thread(target=_reader, daemon=True)
        reader.start()
        while True:
            if proc.poll() is not None:
                break
            if task.get("cancelled"):
                task["log_lines"].append("[工作台] 收到取消请求，正在终止分析...")
                _terminate_proc_tree(proc)
                break
            if time.time() - started > ANALYSIS_TIMEOUT:
                timed_out = True
                task["log_lines"].append(f"[工作台] 分析超过 {ANALYSIS_TIMEOUT // 60} 分钟，已自动终止")
                _terminate_proc_tree(proc)
                break
            time.sleep(0.5)
        reader.join(timeout=5)
        proc.wait(timeout=10)
        task["exit_code"] = proc.returncode
    except Exception as e:
        task["log_lines"].append(f"[工作台] 读取日志异常: {e}")
        try:
            _terminate_proc_tree(proc)
        except Exception:
            pass
        task["exit_code"] = proc.returncode if proc.poll() is not None else -1
    if task.get("cancelled"):
        task["status"] = "error"
        task["log_lines"].append("[工作台] 分析已取消")
    elif timed_out:
        task["status"] = "error"
        task["log_lines"].append("[工作台] 分析超时，请检查网络/VPN 后重试")
    else:
        # 兜底：即使进程退出码非 0，只要本次运行期间生成了该 ticker 的新报告，也视为成功
        ok = (task.get("exit_code") == 0) or _report_generated_since(ticker, started)
        task["status"] = "done" if ok else "error"
    task["finished_at"] = time.time()
    _save_tasks_history()


def _terminate_proc_tree(proc):
    """终止 docker compose 进程及其派生的容器（Windows 上 terminate 掉 compose 进程即可，
    其会向容器发送 stop 信号）。"""
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
    except Exception:
        pass
    # 兜底：直接停掉该 compose 项目下运行中的一次性容器，确保不残留
    try:
        subprocess.run(
            ["docker", "compose", "ps", "-q"],
            cwd=BASE_DIR, capture_output=True, timeout=15,
        )
    except Exception:
        pass


def _report_generated_since(ticker, since_ts):
    """检查 since_ts 之后是否生成了该 ticker 的新报告"""
    try:
        if not os.path.isdir(REPORTS_DIR):
            return False
        t = ticker.upper()
        for d in os.listdir(REPORTS_DIR):
            if not d.upper().startswith(t + "_"):
                continue
            md = os.path.join(REPORTS_DIR, d, "complete_report.md")
            if os.path.isfile(md) and os.path.getmtime(md) >= since_ts:
                return True
    except Exception:
        pass
    return False


def make_task_id():
    return str(int(time.time() * 1000))


class Handler(BaseHTTPRequestHandler):
    server_version = "TradingAgentsWorkbench/1.0"

    def log_message(self, fmt, *args):
        pass  # 静默日志

    # ---- 响应工具 ----
    def _send_json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path, content_type):
        if not os.path.exists(path):
            self.send_error(404)
            return
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        # 静态资源禁用缓存，确保前端 JS/CSS 改动后刷新即生效
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_text(self, text, content_type="text/plain; charset=utf-8"):
        data = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        # 静态资源
        if path == "/" or path == "/index.html":
            return self._send_file(os.path.join(STATIC_DIR, "index.html"), "text/html; charset=utf-8")
        if path.startswith("/static/"):
            rel = path[len("/static/"):]
            safe = os.path.realpath(os.path.join(STATIC_DIR, rel))
            if not safe.startswith(os.path.realpath(STATIC_DIR)):
                return self.send_error(403)
            ct = "text/html; charset=utf-8"
            if rel.endswith(".css"):
                ct = "text/css; charset=utf-8"
            elif rel.endswith(".js"):
                ct = "application/javascript; charset=utf-8"
            elif rel.endswith(".png"):
                ct = "image/png"
            elif rel.endswith(".svg"):
                ct = "image/svg+xml"
            return self._send_file(safe, ct)

        # API：报告列表（grouped=1 时返回按标的聚合的文件夹结构）
        if path == "/api/reports":
            grouped = qs.get("grouped", ["0"])[0] == "1"
            if grouped:
                return self._send_json({"groups": list_report_groups()})
            return self._send_json({"reports": list_reports()})

        # API：定时任务列表
        if path == "/api/schedules":
            with SCHEDULES_LOCK:
                items = [{"ticker": t, **cfg} for t, cfg in SCHEDULES.items()]
            items.sort(key=lambda x: x["ticker"])
            # 补充市场分类与标的名称，供前端分组展示
            for it in items:
                it["market"] = classify_market(it["ticker"])
                it["name"] = get_company_name(it["ticker"])
            return self._send_json({"schedules": items})

        # API：报告阶段树
        if path == "/api/report/stages":
            rid = qs.get("id", [""])[0]
            return self._send_json({"stages": list_report_stages(rid)})

        # API：报告文件内容
        if path == "/api/report/file":
            rid = qs.get("id", [""])[0]
            rel = qs.get("file", [""])[0]
            p = resolve_report_path(rid, rel)
            if not p:
                return self._send_json({"error": "文件不存在"}, 404)
            return self._send_text(read_text(p))

        # API：任务列表
        if path == "/api/tasks":
            with TASKS_LOCK:
                # 合并磁盘历史任务 + 内存运行任务（内存态优先，覆盖同 id）
                merged = dict(TASKS_HISTORY)
                for tid, t in RUNNING_TASKS.items():
                    merged[tid] = {
                        "ticker": t["ticker"],
                        "date": t["date"],
                        "analysts": t["analysts"],
                        "status": t["status"],
                        "exit_code": t.get("exit_code"),
                        "created_at": t["created_at"],
                        "finished_at": t.get("finished_at"),
                    }
            tasks = [{"id": tid, **meta} for tid, meta in merged.items()]
            tasks.sort(key=lambda x: x["created_at"], reverse=True)
            return self._send_json({"tasks": tasks})

        # API：SSE 实时日志
        if path == "/api/task/log":
            tid = qs.get("id", [""])[0]
            return self._handle_sse(tid)

        self.send_error(404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # API：新增/更新定时任务
        if path == "/api/schedules":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8", errors="ignore")
            try:
                req = json.loads(body)
            except Exception:
                return self._send_json({"error": "无效的 JSON"}, 400)
            ticker = (req.get("ticker") or "").strip().upper()
            if not re.match(r"^[\w.\-]{1,20}$", ticker):
                return self._send_json({"error": "股票代码格式不合法"}, 400)
            enabled = bool(req.get("enabled", True))
            try:
                hour = int(req.get("hour", SCHEDULE_HOUR))
                minute = int(req.get("minute", SCHEDULE_MINUTE))
                if not (0 <= hour <= 23 and 0 <= minute <= 59):
                    raise ValueError
            except (TypeError, ValueError):
                return self._send_json({"error": "时间格式不合法"}, 400)
            analysts = req.get("analysts") or "market,sentiment,news,fundamentals"
            valid_analysts = {"market", "sentiment", "news", "fundamentals"}
            analysts = ",".join(a for a in str(analysts).split(",") if a.strip() in valid_analysts)
            if not analysts:
                analysts = "market,sentiment,news,fundamentals"
            with SCHEDULES_LOCK:
                SCHEDULES[ticker] = {
                    "enabled": enabled,
                    "hour": hour,
                    "minute": minute,
                    "analysts": analysts,
                }
                _write_schedules(SCHEDULES)
            return self._send_json({"status": "saved", "ticker": ticker})

        # API：取消分析任务
        if path == "/api/task/cancel":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8", errors="ignore")
            try:
                req = json.loads(body)
            except Exception:
                return self._send_json({"error": "无效的 JSON"}, 400)
            tid = req.get("id") or ""
            with TASKS_LOCK:
                task = RUNNING_TASKS.get(tid)
            if not task:
                return self._send_json({"error": "任务不存在"}, 404)
            if task["status"] in ("done", "error"):
                return self._send_json({"error": "任务已结束，无需取消"}, 400)
            task["cancelled"] = True
            return self._send_json({"status": "cancelling"})

        if path != "/api/analyze":
            return self.send_error(404)
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8", errors="ignore")
        try:
            req = json.loads(body)
        except Exception:
            return self._send_json({"error": "无效的 JSON"}, 400)
        ticker = (req.get("ticker") or "").strip()
        date = (req.get("date") or "").strip()
        analysts = req.get("analysts") or ["market", "sentiment", "news", "fundamentals"]
        if not ticker:
            return self._send_json({"error": "股票代码不能为空"}, 400)
        if not re.match(r"^[\w.\-]{1,20}$", ticker):
            return self._send_json({"error": "股票代码格式不合法"}, 400)
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            return self._send_json({"error": "日期格式应为 YYYY-MM-DD"}, 400)
        valid_analysts = {"market", "sentiment", "news", "fundamentals"}
        analysts = [a for a in analysts if a in valid_analysts]
        if not analysts:
            analysts = ["market", "sentiment", "news", "fundamentals"]

        task_id = make_task_id()
        with TASKS_LOCK:
            RUNNING_TASKS[task_id] = {
                "ticker": ticker,
                "date": date,
                "analysts": ",".join(analysts),
                "status": "starting",
                "log_lines": [],
                "exit_code": None,
                "created_at": time.time(),
                "finished_at": None,
            }
        _save_tasks_history()
        t = threading.Thread(target=run_analysis, args=(ticker, date, ",".join(analysts), task_id), daemon=True)
        t.start()
        return self._send_json({"task_id": task_id, "status": "started"})

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        # 删除单个定时任务
        if path == "/api/schedules":
            ticker = qs.get("ticker", [""])[0].strip().upper()
            if not ticker:
                return self._send_json({"error": "缺少股票代码"}, 400)
            with SCHEDULES_LOCK:
                if ticker not in SCHEDULES:
                    return self._send_json({"error": "定时任务不存在"}, 404)
                del SCHEDULES[ticker]
                _write_schedules(SCHEDULES)
            return self._send_json({"status": "deleted"})

        # 删除单个任务记录
        if path == "/api/task":
            tid = qs.get("id", [""])[0]
            if not tid:
                return self._send_json({"error": "缺少任务 id"}, 400)
            with TASKS_LOCK:
                if tid in RUNNING_TASKS and RUNNING_TASKS[tid].get("status") in ("running", "starting"):
                    return self._send_json({"error": "任务运行中，无法删除"}, 400)
                RUNNING_TASKS.pop(tid, None)
            TASKS_HISTORY.pop(tid, None)
            _write_tasks_file(TASKS_HISTORY)
            return self._send_json({"status": "deleted"})

        # 清空全部任务记录
        if path == "/api/tasks":
            with TASKS_LOCK:
                for tid in list(RUNNING_TASKS.keys()):
                    if RUNNING_TASKS[tid].get("status") in ("running", "starting"):
                        return self._send_json({"error": "存在运行中的任务，无法清空"}, 400)
                RUNNING_TASKS.clear()
            TASKS_HISTORY.clear()
            _write_tasks_file(TASKS_HISTORY)
            return self._send_json({"status": "cleared"})

        # 删除单个报告（连同磁盘目录）
        if path == "/api/report":
            rid = qs.get("id", [""])[0]
            if not rid:
                return self._send_json({"error": "缺少报告 id"}, 400)
            # 安全校验：id 只允许 ticker_日期_时间 格式，防目录穿越
            if not re.fullmatch(r"[A-Za-z0-9.\-]+_\d{8}_\d{6}", rid):
                return self._send_json({"error": "报告 id 格式不合法"}, 400)
            base = os.path.realpath(os.path.join(REPORTS_DIR, rid))
            if not base.startswith(os.path.realpath(REPORTS_DIR) + os.sep):
                return self._send_json({"error": "非法路径"}, 403)
            if not os.path.isdir(base):
                return self._send_json({"error": "报告不存在"}, 404)
            try:
                import shutil
                shutil.rmtree(base)
            except Exception as e:
                return self._send_json({"error": f"删除失败: {e}"}, 500)
            return self._send_json({"status": "deleted"})

        return self.send_error(404)

    def _handle_sse(self, task_id):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        sent = 0
        try:
            while True:
                with TASKS_LOCK:
                    task = RUNNING_TASKS.get(task_id)
                if task is None:
                    self.wfile.write(f"data: {json.dumps({'event':'done','lines':['任务不存在']}, ensure_ascii=False)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    break
                lines = task["log_lines"]
                if len(lines) > sent:
                    new_lines = lines[sent:]
                    sent = len(lines)
                    self.wfile.write(f"data: {json.dumps({'event':'log','lines':new_lines}, ensure_ascii=False)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                if task["status"] in ("done", "error"):
                    self.wfile.write(f"data: {json.dumps({'event':'done','status':task['status'],'exit_code':task.get('exit_code')}, ensure_ascii=False)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    break
                time.sleep(0.5)
        except (BrokenPipeError, ConnectionResetError):
            pass


def main():
    os.makedirs(STATIC_DIR, exist_ok=True)
    # 启动定时任务调度线程
    scheduler = threading.Thread(target=_scheduler_loop, daemon=True)
    scheduler.start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"TradingAgents 工作台已启动: http://127.0.0.1:{PORT}")
    print(f"定时任务调度器已启动（每天 {SCHEDULE_HOUR}:{SCHEDULE_MINUTE:02d} 后触发到点任务）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
