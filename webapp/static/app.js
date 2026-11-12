// TradingAgents 工作台前端逻辑
let currentReportId = null;
let currentView = 'md';
let activeTaskId = null;
let sseSource = null;
let lastAnalyzedTicker = null;

// ===== 工具 =====
function fmtTime(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function ratingBadge(r) {
  const rl = (r || '').toLowerCase();
  if (rl === 'sell') return '<span class="badge badge-sell">卖出</span>';
  if (rl === 'buy') return '<span class="badge badge-buy">买入</span>';
  if (rl === 'hold' || rl === 'neutral') return '<span class="badge badge-hold">持有</span>';
  return r ? `<span class="badge badge-other">${r}</span>` : '';
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (res.status === 404) return null;
  return res.json();
}

// ===== 报告列表（按标的分组为文件夹树） =====
let expandedGroups = {};  // ticker -> true/false，记住展开状态

async function loadReports() {
  const data = await api('/api/reports?grouped=1');
  if (!data) return;
  const list = document.getElementById('reportList');
  const countEl = document.getElementById('reportCount');
  const groups = data.groups || [];
  if (!groups.length) {
    list.innerHTML = '<div style="color:var(--text-faint);font-size:12px;padding:8px;">暂无报告</div>';
    if (countEl) countEl.textContent = '';
    return;
  }
  if (countEl) countEl.textContent = groups.length + ' 个标的';
  list.innerHTML = groups.map(g => {
    const key = g.ticker.toUpperCase();
    const isOpen = expandedGroups[key] !== false; // 默认展开
    const label = g.name ? `${g.ticker} <span class="ri-name">${escapeHtml(g.name)}</span>` : g.ticker;
    const arrow = isOpen ? '▾' : '▸';
    const latestBadge = ratingBadge(g.latest_rating);
    const dailyItems = g.reports.map(r => {
      const active = r.id === currentReportId ? 'active' : '';
      return `
      <div class="report-item sub ${active}">
        <div class="ri-main" onclick="openReport('${r.id}','${r.ticker}')">
          <div class="ri-ticker">${r.date || ''} ${ratingBadge(r.rating)}</div>
        </div>
        <button class="item-del" onclick="deleteReport('${r.id}')" title="删除该报告">✕</button>
      </div>`;
    }).join('');
    return `
    <div class="report-group">
      <div class="group-head" onclick="toggleGroup('${key}')">
        <span class="group-arrow">${arrow}</span>
        <span class="group-title">${label}</span>
        <span class="group-meta">${g.count}份 ${latestBadge}</span>
      </div>
      ${isOpen ? `<div class="group-body">${dailyItems}</div>` : ''}
    </div>`;
  }).join('');
}

function toggleGroup(ticker) {
  expandedGroups[ticker] = expandedGroups[ticker] === false;
  loadReports();
}

// 转义 HTML，防止公司简称/代码注入
function escapeHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

async function deleteReport(id) {
  if (!confirm(`确定删除报告「${id}」吗？此操作会删除磁盘上的报告文件，不可恢复。`)) return;
  const res = await api(`/api/report?id=${encodeURIComponent(id)}`, { method: 'DELETE' });
  if (res && res.error) { alert(res.error); return; }
  if (currentReportId === id) {
    // 若删除的是当前正在查看的报告，回到空状态
    currentReportId = null;
    document.getElementById('reportViewer').style.display = 'none';
    document.getElementById('emptyState').style.display = 'flex';
    document.getElementById('newAnalysis').style.display = 'none';
  }
  loadReports();
  loadTasks();
}

async function loadTasks() {
  const data = await api('/api/tasks');
  if (!data) return;
  const list = document.getElementById('taskList');
  if (!data.tasks.length) {
    list.innerHTML = '<div style="color:var(--text-faint);font-size:12px;padding:8px;">暂无任务</div>';
    return;
  }
  const statusText = { starting: '启动中', running: '运行中', done: '完成', error: '失败' };
  list.innerHTML = data.tasks.map(t => {
    // 已完成/失败：点击打开该 ticker 的最新报告
    // 运行中/启动中：点击切回分析面板并重连实时进度
    let onClick = '';
    let hint = '';
    if (t.status === 'done' || t.status === 'error') {
      onClick = `onclick="openLatestReport('${t.ticker}')"`;
      hint = ' title="点击查看该标的的最新报告"';
    } else if (t.status === 'running' || t.status === 'starting') {
      onClick = `onclick="openRunningTask('${t.id}','${t.ticker}','${t.date}')"`;
      hint = ' title="点击返回分析面板查看实时进度"';
    }
    const cursor = onClick ? 'cursor:pointer;' : '';
    return `
    <div class="task-item" style="${cursor}"${hint}>
      <div class="ti-main" ${onClick}>
        <div class="ti-top">
          <span class="ti-ticker">${t.ticker}</span>
          <span class="task-status ${t.status}">${statusText[t.status] || t.status}</span>
        </div>
        <div class="ti-time">${fmtTime(t.created_at)} · ${t.date}</div>
      </div>
      <button class="item-del" onclick="deleteTask('${t.id}')" title="删除该任务记录">✕</button>
    </div>`;
  }).join('');
}

async function deleteTask(id) {
  if (!confirm('确定删除这条任务记录吗？')) return;
  const res = await api(`/api/task?id=${encodeURIComponent(id)}`, { method: 'DELETE' });
  if (res && res.error) { alert(res.error); return; }
  loadTasks();
}

async function clearAllTasks() {
  if (!confirm('确定清空全部任务记录吗？此操作不影响历史报告。')) return;
  const res = await api('/api/tasks', { method: 'DELETE' });
  if (res && res.error) { alert(res.error); return; }
  loadTasks();
}

// 打开某 ticker 的最新报告（按 mtime 倒序取第一条）
async function openLatestReport(ticker) {
  const data = await api('/api/reports?grouped=1');
  if (!data || !data.groups) return;
  const group = data.groups.find(g => g.ticker.toUpperCase() === ticker.toUpperCase());
  if (group && group.reports.length) {
    openReport(group.reports[0].id, group.reports[0].ticker);
  } else {
    alert(`未找到 ${ticker} 的历史报告`);
  }
}

// 点击运行中/启动中的任务：切回分析面板并重连实时进度
function openRunningTask(taskId, ticker, date) {
  currentReportId = null;
  lastAnalyzedTicker = ticker;
  document.getElementById('emptyState').style.display = 'none';
  document.getElementById('reportViewer').style.display = 'none';
  document.getElementById('newAnalysis').style.display = 'flex';
  // 显示进度卡，并恢复标题与状态
  document.getElementById('progressCard').style.display = 'block';
  document.getElementById('progressTitle').textContent = `分析 ${ticker}（${date}）`;
  document.getElementById('logConsole').textContent = '';
  setProgressStatus('running', '运行中');
  // 重连该任务的实时日志
  activeTaskId = taskId;
  connectSSE(taskId);
  // 恢复"取消"按钮可用
  const cancelBtn = document.getElementById('btnCancel');
  cancelBtn.disabled = false;
  cancelBtn.textContent = '取消分析';
  // 滚动到进度卡
  setTimeout(() => {
    document.getElementById('progressCard').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }, 100);
}

// ===== 报告查看 =====
async function openReport(id, ticker) {
  currentReportId = id;
  currentView = 'md';
  document.getElementById('emptyState').style.display = 'none';
  document.getElementById('newAnalysis').style.display = 'none';
  document.getElementById('schedulesPanel').style.display = 'none';
  document.getElementById('reportViewer').style.display = 'flex';
  document.getElementById('viewerTitle').textContent = `${ticker} 分析报告`;
  switchView('md');
  await loadReports();

  // 加载正文
  const md = await fetch(`/api/report/file?id=${id}&file=complete_report.md`);
  const text = await md.text();
  document.getElementById('mdContent').innerHTML = renderMarkdown(text);

  // 预加载阶段树
  await loadStages(id);
}

async function loadStages(id) {
  const data = await api(`/api/report/stages?id=${id}`);
  if (!data || !data.stages.length) {
    document.getElementById('stagesContainer').innerHTML = '<div style="color:var(--text-faint)">暂无阶段数据</div>';
    return;
  }
  const container = document.getElementById('stagesContainer');
  container.innerHTML = data.stages.map((s, i) => `
    <div class="stage-card ${i === 0 ? 'open' : ''}">
      <div class="stage-head" onclick="toggleStage(this)">
        <span>${s.label}</span>
        <span class="stage-arrow">▶</span>
      </div>
      <div class="stage-files">
        ${s.files.map(f => `<div class="stage-file" onclick="loadStageFile('${id}','${s.dir}/${f.file}','${f.name}')">${f.name}</div>`).join('')}
      </div>
    </div>`).join('');
}

function toggleStage(el) {
  el.parentElement.classList.toggle('open');
}

async function loadStageFile(id, file, name) {
  const res = await fetch(`/api/report/file?id=${id}&file=${file}`);
  const text = await res.text();
  document.getElementById('mdContent').innerHTML = `<h1>${name}</h1>` + renderMarkdown(text);
  // 切换到正文视图展示
  switchView('md');
  // 滚动到顶部
  document.getElementById('viewMd').scrollIntoView({ behavior: 'smooth' });
}

function switchView(view) {
  currentView = view;
  document.getElementById('viewMd').style.display = view === 'md' ? 'block' : 'none';
  document.getElementById('viewStages').style.display = view === 'stages' ? 'block' : 'none';
  document.querySelectorAll('.view-tabs .tab').forEach(t => {
    t.classList.toggle('active', t.dataset.view === view);
  });
}

function openHtml() {
  if (currentReportId) {
    window.open(`/api/report/file?id=${currentReportId}&file=complete_report.html`, '_blank');
  }
}

// ===== 新分析 =====
function showNewAnalysis() {
  currentReportId = null;
  document.getElementById('emptyState').style.display = 'none';
  document.getElementById('reportViewer').style.display = 'none';
  document.getElementById('schedulesPanel').style.display = 'none';
  document.getElementById('newAnalysis').style.display = 'flex';
  document.getElementById('progressCard').style.display = 'none';
  // 默认日期为今天
  const today = new Date();
  const p = (n) => String(n).padStart(2, '0');
  document.getElementById('inpDate').value = `${today.getFullYear()}-${p(today.getMonth()+1)}-${p(today.getDate())}`;
  loadReports();
}

// ===== 市场类型识别 =====
function detectMarket() {
  const raw = document.getElementById('inpTicker').value.trim();
  const el = document.getElementById('marketHint');
  if (!raw) {
    el.className = 'market-hint';
    el.textContent = '';
    return;
  }
  const upper = raw.toUpperCase();
  let cls = '';
  let text = '';
  // 港股：形如 0700.HK / 9988.HK / 700.HK
  if (/^\d{1,5}\.HK$/.test(upper)) {
    cls = 'hk';
    text = '🇭🇰 已识别为港股（走 AkShare 行情/财务/新闻）';
  }
  // 6 位纯数字：先判 ETF，再判 A股
  else if (/^\d{6}$/.test(upper)) {
    const p3 = upper.slice(0, 3);
    const p2 = upper.slice(0, 2);
    // 场内基金/ETF/LOF：上交所 51/52/53/56/58/59，深交所 15/16/18
    const etfSh = ['51', '52', '53', '56', '58', '59'];
    const etfSz = ['15', '16', '18'];
    if (etfSh.includes(p2)) {
      cls = 'etf-sh';
      text = '📈 已识别为场内 ETF/基金（上海，走 AkShare 行情）';
    }
    else if (etfSz.includes(p2)) {
      cls = 'etf-sz';
      text = '📈 已识别为场内 ETF/基金（深圳，走 AkShare 行情）';
    }
    else {
      const sh = ['600', '601', '603', '605', '688', '689'];
      const sz = ['000', '001', '002', '003', '300', '301'];
      if (sh.includes(p3)) { cls = 'a-sh'; text = '🇨🇳 已识别为 A股（上海，走 AkShare）'; }
      else if (sz.includes(p3)) { cls = 'a-sz'; text = '🇨🇳 已识别为 A股（深圳，走 AkShare）'; }
      else { cls = 'a-oth'; text = '🇨🇳 已识别为 A股（走 AkShare）'; }
    }
  }
  // 美股 / 其它：英文字母代码
  else if (/^[A-Z]{1,5}(\.[A-Z]{1,3})?$/.test(upper)) {
    cls = 'us';
    text = '🇺🇸 已识别为美股（走 yfinance/SEC/FRED 等）';
  }
  else {
    cls = 'unknown';
    text = '⚠️ 代码格式可能不常见，请确认后继续';
  }
  el.className = 'market-hint ' + cls;
  el.textContent = text;
}

async function startAnalysis() {
  const ticker = document.getElementById('inpTicker').value.trim();
  const date = document.getElementById('inpDate').value;
  if (!ticker) { alert('请输入股票代码'); return; }
  if (!date) { alert('请选择分析日期'); return; }
  const analysts = Array.from(document.querySelectorAll('.analyst-options input:checked')).map(c => c.value);

  const btn = document.getElementById('btnStart');
  btn.disabled = true;
  btn.textContent = '启动中...';

  try {
    const res = await fetch('/api/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ticker, date, analysts })
    });
    const data = await res.json();
    if (data.error) { alert(data.error); btn.disabled = false; btn.textContent = '开始分析'; return; }

    activeTaskId = data.task_id;
    lastAnalyzedTicker = ticker;
    // 显示进度卡
    document.getElementById('progressCard').style.display = 'block';
    document.getElementById('progressTitle').textContent = `分析 ${ticker}（${date}）`;
    document.getElementById('logConsole').textContent = '';
    setProgressStatus('running', '运行中');
    const cancelBtn = document.getElementById('btnCancel');
    cancelBtn.disabled = false;
    cancelBtn.textContent = '取消分析';
    connectSSE(data.task_id);
    loadTasks();
    // 滚动到进度卡，确保运行时用户能看到实时日志
    setTimeout(() => {
      document.getElementById('progressCard').scrollIntoView({ behavior: 'smooth', block: 'start' });
    }, 100);
  } catch (e) {
    alert('请求失败: ' + e.message);
    btn.disabled = false;
    btn.textContent = '开始分析';
  }
}

function setProgressStatus(status, text) {
  const el = document.getElementById('progressStatus');
  el.className = 'progress-status ' + status;
  el.textContent = text;
}

async function cancelAnalysis() {
  if (!activeTaskId) return;
  const btn = document.getElementById('btnCancel');
  if (!confirm('确定要取消当前分析吗？')) return;
  btn.disabled = true;
  btn.textContent = '取消中...';
  try {
    await fetch('/api/task/cancel', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: activeTaskId })
    });
  } catch (e) {
    alert('取消失败: ' + e.message);
    btn.disabled = false;
    btn.textContent = '取消分析';
  }
}

function connectSSE(taskId) {
  if (sseSource) sseSource.close();
  sseSource = new EventSource(`/api/task/log?id=${taskId}`);
  const consoleEl = document.getElementById('logConsole');

  sseSource.onmessage = (e) => {
    try {
      const msg = JSON.parse(e.data);
      if (msg.event === 'log' && msg.lines) {
        // 兜底清理 ANSI 转义码与控制字符（后端已清理，这里再保险一次）
        const clean = msg.lines.map(l =>
          String(l).replace(/\x1b\[[0-9;?]*[a-zA-Z]/g, '').replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, '')
        ).join('\n');
        consoleEl.textContent += clean + '\n';
        consoleEl.scrollTop = consoleEl.scrollHeight;
      } else if (msg.event === 'done') {
        sseSource.close();
        sseSource = null;
        setProgressStatus(msg.status === 'done' ? 'done' : 'error', msg.status === 'done' ? '完成' : '失败');
        const btn = document.getElementById('btnStart');
        btn.disabled = false;
        btn.textContent = '再次分析';
        const cancelBtn = document.getElementById('btnCancel');
        cancelBtn.disabled = true;
        cancelBtn.textContent = '取消分析';
        activeTaskId = null; // 停止轮询
        loadTasks();
        loadReports();
        if (msg.status === 'done') {
          // 提示并自动刷新报告列表，延迟后自动打开最新报告
          setTimeout(async () => {
            await loadReports();
            await openLatestReport(lastAnalyzedTicker);
          }, 1500);
        }
      }
    } catch (err) {}
  };
  sseSource.onerror = () => {
    // SSE 连接错误时，静默重连由 EventSource 自动处理
  };
}

// ===== Markdown 渲染（轻量） =====
function inline(s) {
  return s
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\*([^*]+)\*/g, '<em>$1</em>')
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank">$1</a>');
}

function renderMarkdown(md) {
  const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const lines = md.split('\n');
  const out = [];
  let i = 0;
  let listOpen = false; // 'ul' | 'ol' | null

  const closeList = () => {
    if (listOpen) { out.push(`</${listOpen}>`); listOpen = null; }
  };

  while (i < lines.length) {
    const line = lines[i];

    // 代码块
    if (/^```/.test(line)) {
      closeList();
      const buf = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) { buf.push(lines[i]); i++; }
      i++; // 跳过结束 ```
      out.push(`<pre><code>${esc(buf.join('\n'))}</code></pre>`);
      continue;
    }
    // 标题
    const hm = line.match(/^(#{1,6})\s+(.*)$/);
    if (hm) {
      closeList();
      const lv = hm[1].length;
      out.push(`<h${lv}>${inline(hm[2])}</h${lv}>`);
      i++; continue;
    }
    // 分割线
    if (/^\s*(---+|\*\*\*+)\s*$/.test(line)) {
      closeList();
      out.push('<hr>');
      i++; continue;
    }
    // 引用
    if (/^\s*>\s?/.test(line)) {
      closeList();
      const q = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        q.push(lines[i].replace(/^\s*>\s?/, ''));
        i++;
      }
      out.push(`<blockquote>${inline(q.join('<br>'))}</blockquote>`);
      continue;
    }
    // 表格
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|[\s\-:|]+\|\s*$/.test(lines[i + 1])) {
      closeList();
      const parseRow = (r) => r.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => c.trim());
      const header = parseRow(line);
      out.push('<table><thead><tr>' + header.map(h => `<th>${inline(h)}</th>`).join('') + '</tr></thead><tbody>');
      i += 2; // 跳过表头分隔行
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) {
        const cells = parseRow(lines[i]);
        out.push('<tr>' + cells.map(c => `<td>${inline(c)}</td>`).join('') + '</tr>');
        i++;
      }
      out.push('</tbody></table>');
      continue;
    }
    // 无序列表
    if (/^\s*[-*+]\s+/.test(line)) {
      if (listOpen !== 'ul') { closeList(); out.push('<ul>'); listOpen = 'ul'; }
      out.push(`<li>${inline(line.replace(/^\s*[-*+]\s+/, ''))}</li>`);
      i++; continue;
    }
    // 有序列表
    if (/^\s*\d+\.\s+/.test(line)) {
      if (listOpen !== 'ol') { closeList(); out.push('<ol>'); listOpen = 'ol'; }
      out.push(`<li>${inline(line.replace(/^\s*\d+\.\s+/, ''))}</li>`);
      i++; continue;
    }
    // 空行
    if (line.trim() === '') {
      closeList();
      i++; continue;
    }
    // 普通段落
    closeList();
    out.push(`<p>${inline(line)}</p>`);
    i++;
  }
  closeList();
  return out.join('\n');
}

// ===== 定时刷新任务列表 + 轮询兜底 =====
setInterval(() => {
  const el = document.getElementById('newAnalysis');
  if (el.style.display === 'flex') loadTasks();
  // 兜底：如果有活动任务，轮询检查其状态，避免 SSE 断开后卡在"运行中"
  if (activeTaskId) pollTaskStatus();
}, 5000);

async function pollTaskStatus() {
  try {
    const data = await api('/api/tasks');
    if (!data) return;
    const task = data.tasks.find(t => t.id === activeTaskId);
    if (task && (task.status === 'done' || task.status === 'error')) {
      // 任务已结束，更新 UI
      setProgressStatus(task.status === 'done' ? 'done' : 'error', task.status === 'done' ? '完成' : '失败');
      const btn = document.getElementById('btnStart');
      btn.disabled = false;
      btn.textContent = '再次分析';
      activeTaskId = null; // 停止轮询
      if (sseSource) { sseSource.close(); sseSource = null; }
      loadTasks();
      loadReports();
      if (task.status === 'done' && lastAnalyzedTicker) {
        setTimeout(async () => { await loadReports(); await openLatestReport(lastAnalyzedTicker); }, 1200);
      }
    }
  } catch (e) {}
}

// ===== 定时任务管理 =====
function openSchedules() {
  currentReportId = null;
  document.getElementById('emptyState').style.display = 'none';
  document.getElementById('reportViewer').style.display = 'none';
  document.getElementById('newAnalysis').style.display = 'none';
  document.getElementById('progressCard').style.display = 'none';
  document.getElementById('schedulesPanel').style.display = 'flex';
  loadSchedules();
}

function closeSchedules() {
  document.getElementById('schedulesPanel').style.display = 'none';
  document.getElementById('emptyState').style.display = 'flex';
}

async function loadSchedules() {
  const data = await api('/api/schedules');
  if (!data) return;
  const list = document.getElementById('scheduleList');
  if (!data.schedules.length) {
    list.innerHTML = '<div style="color:var(--text-faint);font-size:12px;padding:12px;">暂无定时任务，上方添加标的即可</div>';
    return;
  }
  list.innerHTML = data.schedules.map(s => {
    const hh = String(s.hour).padStart(2, '0');
    const mm = String(s.minute).padStart(2, '0');
    const toggle = s.enabled ? '已启用' : '已停用';
    return `
    <div class="schedule-item">
      <div class="si-main">
        <div class="si-top">
          <span class="si-ticker">${escapeHtml(s.ticker)}</span>
          <span class="si-time">每天 ${hh}:${mm}</span>
          <span class="task-status ${s.enabled ? 'done' : 'error'}">${toggle}</span>
        </div>
        <div class="si-meta">分析师：${escapeHtml(s.analysts)}</div>
      </div>
      <div class="si-actions">
        <button class="btn-ghost" onclick="toggleSchedule('${s.ticker}', ${!s.enabled})">${s.enabled ? '停用' : '启用'}</button>
        <button class="item-del" onclick="deleteSchedule('${s.ticker}')" title="删除该定时任务">✕</button>
      </div>
    </div>`;
  }).join('');
}

async function addSchedule() {
  const ticker = document.getElementById('inpSchedTicker').value.trim();
  const hour = parseInt(document.getElementById('inpSchedHour').value, 10);
  if (!ticker) { alert('请输入股票代码'); return; }
  const res = await api('/api/schedules', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ticker, hour, minute: 0, enabled: true, analysts: 'market,sentiment,news,fundamentals' })
  });
  if (res && res.error) { alert(res.error); return; }
  document.getElementById('inpSchedTicker').value = '';
  loadSchedules();
}

async function deleteSchedule(ticker) {
  if (!confirm(`确定删除 ${ticker} 的定时任务吗？`)) return;
  const res = await api(`/api/schedules?ticker=${encodeURIComponent(ticker)}`, { method: 'DELETE' });
  if (res && res.error) { alert(res.error); return; }
  loadSchedules();
}

async function toggleSchedule(ticker, enabled) {
  // 读取现有配置再回写，保持 hour/minute/analysts 不变
  const data = await api('/api/schedules');
  const cur = (data.schedules || []).find(s => s.ticker.toUpperCase() === ticker.toUpperCase());
  if (!cur) { loadSchedules(); return; }
  const res = await api('/api/schedules', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ticker, hour: cur.hour, minute: cur.minute, enabled, analysts: cur.analysts })
  });
  if (res && res.error) { alert(res.error); return; }
  loadSchedules();
}

// 初始化
loadReports();
loadTasks();
