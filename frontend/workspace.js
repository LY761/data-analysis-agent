(function () {
  const STORAGE_KEY = 'ecommerce_workbench_context';
  const API = window.WorkbenchApi;
  const moduleNames = {
    dashboard: '经营驾驶舱', chat: '智能问数', products: '商品运营',
    capabilities: '业务能力地图', reports: '报告与历史', automation: '自动化中心',
    data: '数据管理', integrations: '企业接入', evaluation: 'Agent 评测',
  };
  const moduleDescriptions = {
    dashboard: '统一查看核心指标、趋势、经营异常与待办任务。',
    chat: '用自然语言查询经营数据，结果可追溯到 SQL、口径和数据证据。',
    products: '基于确定性指标和规则标签定位商品机会与经营风险。',
    capabilities: '展示领域服务、简单工作流与 Agent 的职责边界。',
    reports: '生成可追溯、可版本化、可导出的经营分析报告。',
    automation: '运行可审计、可重试、具备幂等控制的业务工作流。',
    data: '完成数据预检、字段映射、标准化和质量快照管理。',
    integrations: '通过 REST API 与 MCP 向企业系统开放稳定能力契约。',
    evaluation: '用固定案例持续评估路由、SQL 安全、延迟与回归质量。',
  };
  const moduleRoutes = {
    operation_dashboard: 'dashboard', intelligent_query: 'chat', product_operations: 'products',
    inventory_supply: 'automation', report_alert: 'reports', automation_center: 'automation',
    data_management: 'data',
  };
  const runtimeLabels = {domain_service: '确定性服务', workflow: '简单工作流', agent: 'Agent'};
  const availabilityLabels = {available: '可用', partial: '部分可用', planned: '规划中', reserved: '接口保留'};
  let state = {tenantId: 'olist-demo', current: [], previous: [], module: 'chat'};
  let selectedFile = null;
  let inspection = null;

  try { state = {...state, ...JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}')}; } catch (_) {}

  const escape = value => String(value ?? '').replace(/[&<>'"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
  const format = (value, unit) => {
    if (value === null || value === undefined) return '不可计算';
    const number = Number(value);
    const shown = Number.isFinite(number) ? number.toLocaleString('zh-CN', {maximumFractionDigits: 2}) : value;
    return unit === 'percent' ? shown + '%' : shown;
  };
  const save = () => localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  const content = () => document.getElementById('moduleContent');
  const toolbar = () => document.getElementById('moduleToolbar');
  const loading = (text = '正在加载业务数据…') => { content().innerHTML = `<div class="module-state loading-state"><span class="state-spinner"></span>${escape(text)}</div>`; };
  const empty = (title, detail, action = '') => { content().innerHTML = `<div class="module-state"><strong>${escape(title)}</strong><p>${escape(detail)}</p>${action}</div>`; };
  const error = message => { content().innerHTML = `<div class="module-state error"><strong>请求失败</strong><p>${escape(message)}</p><button class="module-action" onclick="workbench.reload()">重新加载</button></div>`; };
  const badge = (text, tone = '') => `<span class="module-badge ${escape(tone)}">${escape(text)}</span>`;

  function snapshotParams(extra = {}) {
    return {tenant_id: state.tenantId, current_snapshot_ids: state.current, previous_snapshot_ids: state.previous, ...extra};
  }

  function renderToolbar(extra = '') {
    toolbar().innerHTML = `
      <label class="toolbar-field"><span>租户</span><input id="wbTenant" value="${escape(state.tenantId)}" aria-label="租户 ID"></label>
      ${badge(`当前快照 ${state.current.length}`, state.current.length ? 'success' : 'warning')}
      ${badge(`上一周期 ${state.previous.length}`, state.previous.length ? 'success' : '')}
      <button onclick="workbench.autoSnapshots()">选择最新两期</button>
      <button onclick="workbench.reload()" class="primary">刷新</button>${extra}`;
    document.getElementById('wbTenant').addEventListener('change', event => {
      state.tenantId = event.target.value.trim() || 'default';
      state.current = [];
      state.previous = [];
      inspection = null;
      save();
    });
  }

  function periodOf(snapshot) {
    const match = String(snapshot.source_name || '').match(/(?:^|:)(\d{4}-\d{2})(?::|$)/);
    return match ? match[1] : '';
  }

  async function autoSnapshots() {
    const result = await API.query('/standardization/snapshots', {tenant_id: state.tenantId, limit: 500});
    const snapshots = result.snapshots || [];
    const periods = [...new Set(snapshots.map(periodOf).filter(Boolean))].sort().reverse();
    if (periods.length) {
      state.current = snapshots.filter(item => periodOf(item) === periods[0]).map(item => item.snapshot_id);
      state.previous = snapshots.filter(item => periodOf(item) === periods[1]).map(item => item.snapshot_id);
    } else {
      const grouped = {};
      snapshots.forEach(snapshot => (grouped[snapshot.entity_type] ||= []).push(snapshot.snapshot_id));
      state.current = Object.values(grouped).map(ids => ids[0]).filter(Boolean);
      state.previous = Object.values(grouped).map(ids => ids[1]).filter(Boolean);
    }
    save();
    if (!state.current.length) {
      empty('尚无标准快照', '请在数据管理中导入 CSV/Excel，或按 README 导入 Olist 数据。', '<button class="module-action primary" onclick="workbench.openModule(\'data\')">前往数据管理</button>');
      return;
    }
    await loadModule(state.module);
  }

  function requireSnapshots() {
    if (state.current.length) return true;
    empty('需要标准数据快照', '先导入 Olist 或业务 CSV，再选择最新两期。', '<button class="module-action primary" onclick="workbench.openModule(\'data\')">导入数据</button>');
    return false;
  }

  function trendItem(name, values, unit = '') {
    const previous = values?.[0];
    const current = values?.[1];
    const delta = previous ? ((Number(current || 0) - Number(previous)) / Math.abs(Number(previous)) * 100) : null;
    const tone = delta === null ? '' : delta >= 0 ? 'positive' : 'negative';
    return `<div class="trend-row"><span>${escape(name)}</span><span>${format(previous, unit)} → <strong>${format(current, unit)}</strong></span><span class="trend-delta ${tone}">${delta === null ? '—' : `${delta >= 0 ? '+' : ''}${delta.toFixed(1)}%`}</span></div>`;
  }

  async function loadDashboard() {
    renderToolbar();
    if (!requireSnapshots()) return;
    loading('正在聚合指标、异常和任务…');
    const dashboard = await API.query('/dashboard/workbench', snapshotParams({anomaly_limit: 50}));
    const cards = (dashboard.overview.cards || []).map(card => `
      <article class="module-card metric-card status-${escape(card.status)}">
        <div class="card-heading"><span>${escape(card.name)}</span>${badge(`v${card.metric_version}`)}</div>
        <div class="metric-value">${format(card.value, card.unit)}</div>
        <div class="metric-meta">环比 ${format(card.change_pct, 'percent')} · ${escape(card.status)}</div>
        <details><summary>指标口径与证据</summary><p>${escape(card.unavailable_reason || card.formula)}</p><code>${escape(card.evidence.snapshot_ids.join(', '))}</code></details>
      </article>`).join('');
    const productRows = (dashboard.products.top || []).map(item => `<tr><td>${item.rank}</td><td>${escape(item.product_name)}</td><td>${format(item.score)}</td></tr>`).join('');
    const anomalyRows = (dashboard.anomalies || []).slice(0, 8).map(item => `<tr><td>${escape(item.product_name || item.title || item.type)}</td><td>${badge(item.severity, item.severity)}</td><td>${escape((item.tags || []).map(tag => tag.name).join('、') || item.type)}</td></tr>`).join('');
    const series = dashboard.trends.series || {};
    content().innerHTML = `<div class="module-grid">${cards}
      <article class="module-card wide"><div class="card-heading"><h3>周期趋势</h3>${badge('确定性计算', 'success')}</div><div class="trend-list">${trendItem('GMV', series.gmv)}${trendItem('经营毛利', series.gross_profit)}${trendItem('退款金额率', series.refund_rate_pct, 'percent')}${trendItem('平均评分', series.average_rating)}</div></article>
      <article class="module-card wide"><div class="card-heading"><h3>经营状态</h3>${badge(`${dashboard.overview.product_count} 个商品`)}</div><div class="summary-strip"><span><strong>${dashboard.overview.high_risk_product_count}</strong>高风险商品</span><span><strong>${dashboard.tasks.length}</strong>待办任务</span><span><strong>${dashboard.anomaly_count}</strong>异常</span></div><p class="module-muted">已覆盖实体：${escape(dashboard.data_availability.available_entities.join('、') || '无')}</p></article>
      <article class="module-card wide"><h3>商品竞争力排名</h3><div class="table-wrap"><table class="module-table"><tr><th>排名</th><th>商品</th><th>得分</th></tr>${productRows || '<tr><td colspan="3">暂无商品数据</td></tr>'}</table></div></article>
      <article class="module-card wide"><h3>异常与风险</h3><div class="table-wrap"><table class="module-table"><tr><th>对象</th><th>等级</th><th>原因</th></tr>${anomalyRows || '<tr><td colspan="3">当前未发现异常</td></tr>'}</table></div></article>
    </div>`;
  }

  async function loadProducts() {
    renderToolbar();
    if (!requireSnapshots()) return;
    loading('正在生成商品与 SKU 诊断…');
    const data = await API.json('/diagnostics/products', snapshotParams());
    const profiles = data.diagnosis.profiles || [];
    const rows = profiles.map(item => `<tr><td><strong>${escape(item.product_name)}</strong><div class="module-muted">${escape(item.product_id)} · ${escape(item.category || '未分类')}</div></td><td>${format(item.metrics.gmv)}</td><td>${format(item.metrics.gross_margin_pct, 'percent')}</td><td>${format(item.metrics.available_stock)}</td><td>${badge(item.risk_level, item.risk_level)}</td><td>${item.tags.map(tag => badge(tag.name, tag.level)).join('')}</td></tr>`).join('');
    const recommendations = profiles.filter(item => item.risk_level !== 'low').slice(0, 6).map(item => `<article class="insight-item"><strong>${escape(item.product_name)}</strong><p>${escape(item.recommendations.join('；'))}</p></article>`).join('');
    content().innerHTML = `<div class="module-grid"><article class="module-card full"><div class="card-heading"><h3>商品与 SKU 经营画像</h3>${badge('规则服务 L1', 'success')}</div><div class="table-wrap"><table class="module-table"><tr><th>商品</th><th>GMV</th><th>毛利率</th><th>库存</th><th>风险</th><th>规则标签</th></tr>${rows || '<tr><td colspan="6">暂无商品数据</td></tr>'}</table></div></article><article class="module-card wide"><h3>策略建议</h3>${recommendations || '<p class="module-muted">当前无高风险商品建议。</p>'}</article><article class="module-card wide"><h3>职责边界</h3><p>数值与标签由确定性服务计算；补货和复核由工作流建任务；Agent 只负责自然语言解释，不直接修改库存或价格。</p></article></div>`;
  }

  async function loadCapabilities() {
    renderToolbar();
    loading('正在读取统一能力注册表…');
    const catalog = await API.request('/capabilities');
    const summary = catalog.summary || {};
    const cards = (catalog.modules || []).map(item => {
      const route = moduleRoutes[item.id] || 'capabilities';
      const runtimeBadges = item.runtimes.map(runtime => badge(runtimeLabels[runtime] || runtime, runtime)).join('');
      return `<article class="module-card capability-card availability-${escape(item.availability)}"><div class="card-heading"><h3>${escape(item.name)}</h3>${badge(item.maturity, item.availability)}</div><p>${escape(item.description)}</p><div class="badge-row">${runtimeBadges}</div><div class="capability-footer"><span>${availabilityLabels[item.availability] || item.availability}</span><button class="module-action" onclick="workbench.openModule('${route}')">打开入口</button></div></article>`;
    }).join('');
    content().innerHTML = `<div class="architecture-note"><strong>运行时选择原则</strong><span>确定性指标 → 领域服务</span><span>固定状态流转 → 简单工作流</span><span>模糊理解与研究 → Agent</span><span>Agent 不直接执行外部副作用</span></div><div class="summary-strip catalog-summary"><span><strong>${summary.module_count || 0}</strong>一级模块</span><span><strong>${summary.capability_count || 0}</strong>注册能力</span><span><strong>${summary.runtime_counts?.domain_service || 0}</strong>领域服务</span><span><strong>${summary.runtime_counts?.workflow || 0}</strong>工作流</span><span><strong>${summary.runtime_counts?.agent || 0}</strong>Agent</span></div><div class="module-grid capability-grid">${cards}</div>`;
  }

  async function loadReports() {
    renderToolbar('<button onclick="workbench.generateReport()">生成月报</button>');
    loading();
    const [reports, history] = await Promise.all([API.query('/reports', {tenant_id: state.tenantId}), API.query('/analysis-runs', {tenant_id: state.tenantId, limit: 20})]);
    const reportRows = (reports.reports || []).map(item => `<tr><td>${escape(item.title)}</td><td>${escape(item.report_type)}</td><td>${escape(item.period_key)}</td><td>v${item.latest_version}</td><td><a class="table-link" href="/api/reports/${item.report_id}/export?tenant_id=${encodeURIComponent(state.tenantId)}">导出 Excel</a></td></tr>`).join('');
    const runRows = (history.runs || []).map(item => `<tr><td>${escape(item.scenario)}</td><td>${escape(item.status)}</td><td>${escape(item.created_at)}</td><td><code>${escape(item.run_id.slice(0, 10))}</code></td></tr>`).join('');
    content().innerHTML = `<div class="module-grid"><article class="module-card full"><h3>版本化经营报告</h3><div class="table-wrap"><table class="module-table"><tr><th>标题</th><th>类型</th><th>周期</th><th>版本</th><th>操作</th></tr>${reportRows || '<tr><td colspan="5">暂无报告，可基于当前快照生成。</td></tr>'}</table></div></article><article class="module-card full"><h3>分析运行历史</h3><div class="table-wrap"><table class="module-table"><tr><th>场景</th><th>状态</th><th>时间</th><th>运行 ID</th></tr>${runRows || '<tr><td colspan="4">暂无分析记录</td></tr>'}</table></div></article></div>`;
  }

  async function generateReport() {
    if (!requireSnapshots()) return;
    loading('正在生成可追溯月报…');
    await API.json('/reports/generate', {...snapshotParams(), report_type: 'monthly', period_key: new Date().toISOString().slice(0, 7)});
    await loadReports();
  }

  async function loadAutomation() {
    renderToolbar('<button onclick="workbench.runWorkflow(\'low_stock_task\')">运行低库存流程</button>');
    loading();
    const [flows, tasks] = await Promise.all([API.query('/workflows', {tenant_id: state.tenantId}), API.query('/tasks', {tenant_id: state.tenantId})]);
    const flowCards = (flows.workflows || []).map(item => `<article class="workflow-card"><div><strong>${escape(item.name)}</strong><p>${escape(item.description)}</p></div><div>${badge(item.enabled ? '已启用' : '已停用', item.enabled ? 'success' : '')}${badge(`${item.steps.length} 步`)}</div><div class="workflow-steps">${item.steps.map(step => `<span>${escape(step.type)}</span>`).join('<i>→</i>')}</div><button class="module-action" onclick="workbench.runWorkflow('${escape(item.workflow_id)}')">手动运行</button></article>`).join('');
    const taskRows = (tasks.tasks || []).map(item => `<tr><td>${escape(item.title)}</td><td>${badge(item.status, item.status)}</td><td>${escape(item.task_type)}</td><td>${escape(item.created_at)}</td></tr>`).join('');
    content().innerHTML = `<div class="module-grid"><article class="module-card full"><div class="card-heading"><h3>可审计简单工作流</h3>${badge('Agent 不控制状态流转', 'success')}</div><div class="workflow-list">${flowCards}</div></article><article class="module-card full"><h3>业务任务</h3><div class="table-wrap"><table class="module-table"><tr><th>任务</th><th>状态</th><th>类型</th><th>创建时间</th></tr>${taskRows || '<tr><td colspan="4">暂无任务</td></tr>'}</table></div></article></div>`;
  }

  async function runWorkflow(workflowId) {
    if (!requireSnapshots()) return;
    loading(`正在运行 ${workflowId}…`);
    try {
      await API.json(`/workflows/${encodeURIComponent(workflowId)}/run`, {tenant_id: state.tenantId, idempotency_key: `web:${workflowId}:${Date.now()}`, inputs: {current_snapshot_ids: state.current, previous_snapshot_ids: state.previous, business_date: new Date().toISOString().slice(0, 10)}});
      await loadAutomation();
    } catch (exception) { error(exception.message); }
  }

  function importPanel(entities) {
    return `<article class="module-card full import-card"><div class="card-heading"><h3>标准数据导入向导</h3>${badge('上传 → 预检 → 映射 → 快照')}</div><div class="import-controls"><label class="toolbar-field"><span>标准实体</span><select id="dataEntity">${entities.map(item => `<option value="${escape(item.key)}">${escape(item.name)} · ${escape(item.grain)}</option>`).join('')}</select></label><label class="file-picker"><input id="dataFile" type="file" accept=".csv,.xlsx"><span>选择 CSV / Excel</span></label><button class="module-action primary" onclick="workbench.inspectImport()">1. 预检文件</button></div><div id="importResult" class="import-result"><p class="module-muted">文件仅在确认导入后写入标准数据层；预检会检查映射、必填字段、格式和重复主键。</p></div></article>`;
  }

  async function loadData() {
    renderToolbar();
    loading();
    const [snapshotData, entityData] = await Promise.all([API.query('/standardization/snapshots', {tenant_id: state.tenantId, limit: 500}), API.request('/semantic/entities')]);
    const rows = (snapshotData.snapshots || []).map(item => `<tr><td>${escape(item.entity_type)}</td><td>${escape(item.source_name)}</td><td>${format(item.row_count)}</td><td>${format(item.quality_score)}</td><td>${badge(item.quality_status, item.quality_status)}</td><td>${escape(item.created_at)}</td></tr>`).join('');
    content().innerHTML = `<div class="module-grid">${importPanel(entityData.entities || [])}<article class="module-card full"><div class="card-heading"><h3>标准数据快照</h3>${badge(`${(snapshotData.snapshots || []).length} 个`)}</div><div class="table-wrap"><table class="module-table"><tr><th>实体</th><th>来源</th><th>行数</th><th>质量分</th><th>状态</th><th>创建时间</th></tr>${rows || '<tr><td colspan="6">暂无快照</td></tr>'}</table></div></article></div>`;
    document.getElementById('dataFile').addEventListener('change', event => { selectedFile = event.target.files[0] || null; inspection = null; });
  }

  async function inspectImport() {
    const resultNode = document.getElementById('importResult');
    const entity = document.getElementById('dataEntity').value;
    if (!selectedFile) { resultNode.innerHTML = '<p class="inline-error">请先选择 CSV 或 Excel 文件。</p>'; return; }
    resultNode.innerHTML = '<div class="inline-loading"><span class="state-spinner"></span>正在预检…</div>';
    const form = new FormData();
    form.append('file', selectedFile);
    form.append('entity_type', entity);
    try {
      inspection = await API.upload('/data-quality/inspect-file', form);
      const quality = inspection.quality;
      const issues = (quality.issues || []).map(item => `<li>${badge(item.severity, item.severity)} ${escape(item.message)}</li>`).join('');
      resultNode.innerHTML = `<div class="quality-summary"><strong>质量分 ${format(quality.score)}</strong>${badge(quality.status, quality.status)}<span>${quality.row_count} 行 · ${quality.issue_count} 个问题</span></div><label class="mapping-editor"><span>字段映射（源字段 → 标准字段）</span><textarea id="mappingJson" rows="7">${escape(JSON.stringify(quality.mapping, null, 2))}</textarea></label>${issues ? `<ul class="issue-list">${issues}</ul>` : '<p class="success-text">预检通过，未发现质量问题。</p>'}<button class="module-action primary" onclick="workbench.confirmImport()">2. 确认映射并导入</button>`;
    } catch (exception) { resultNode.innerHTML = `<p class="inline-error">${escape(exception.message)}</p>`; }
  }

  async function confirmImport() {
    const resultNode = document.getElementById('importResult');
    if (!selectedFile || !inspection) { resultNode.innerHTML = '<p class="inline-error">请先完成文件预检。</p>'; return; }
    let mapping;
    try { mapping = JSON.parse(document.getElementById('mappingJson').value); } catch (_) { resultNode.insertAdjacentHTML('afterbegin', '<p class="inline-error">字段映射不是有效 JSON。</p>'); return; }
    const form = new FormData();
    form.append('file', selectedFile);
    form.append('entity_type', document.getElementById('dataEntity').value);
    form.append('mapping_json', JSON.stringify(mapping));
    form.append('tenant_id', state.tenantId);
    form.append('allow_warning', 'true');
    resultNode.innerHTML = '<div class="inline-loading"><span class="state-spinner"></span>正在创建标准快照…</div>';
    try {
      const imported = await API.upload('/standardization/import-file', form);
      state.current = [];
      state.previous = [];
      selectedFile = null;
      inspection = null;
      save();
      resultNode.innerHTML = `<p class="success-text">导入完成：${escape(imported.snapshot?.snapshot_id || imported.snapshot_id || '已创建快照')}</p>`;
      setTimeout(() => loadData(), 700);
    } catch (exception) { resultNode.innerHTML = `<p class="inline-error">${escape(exception.message)}</p>`; }
  }

  async function loadIntegrations() {
    renderToolbar();
    loading('正在读取对外能力契约…');
    const catalog = await API.request('/capabilities');
    const connectorCards = [
      ['REST API', 'available', '/api/integrations/v1/*', '企业系统与客服网关的版本化接口'],
      ['MCP', 'available', 'stdio / Streamable HTTP', '供外部 Agent 调用的工具协议'],
      ['customer-service-agent', 'reserved', '网关适配', '保留客服项目调用契约，不耦合部署'],
      ['钉钉 / 飞书 / 企业微信', 'reserved', '消息适配器', '保留通知、卡片和机器人入口'],
      ['淘宝 / 京东 / 微信客服', 'reserved', '平台连接器', '等待真实商家授权后启用'],
    ].map(item => `<article class="module-card connector-card"><div class="card-heading"><h3>${item[0]}</h3>${badge(availabilityLabels[item[1]], item[1])}</div><code>${escape(item[2])}</code><p>${escape(item[3])}</p></article>`).join('');
    content().innerHTML = `<div class="architecture-note"><strong>企业接入原则</strong><span>核心能力与渠道解耦</span><span>统一 tenant_id / request_id</span><span>高风险动作必须审批</span><span>未授权连接器不假执行</span></div><div class="module-grid">${connectorCards}<article class="module-card full"><h3>稳定调用契约</h3><p>当前注册 ${catalog.summary.capability_count} 项能力。Web、REST、MCP 与客服网关共用同一能力注册表和确定性业务服务。</p><div class="endpoint-list"><code>GET /api/capabilities</code><code>POST /api/capabilities/{id}/execute</code><code>GET /api/integrations/v1/capabilities</code><code>python backend/mcp_server.py</code></div></article></div>`;
  }

  async function loadEvaluation() {
    renderToolbar('<button onclick="workbench.runEvaluation()">运行内部评测</button>');
    loading();
    const data = await API.request('/evaluations/golden-scenarios');
    content().innerHTML = `<div class="architecture-note"><strong>评测边界</strong><span>固定路由集与 SQL 安全集</span><span>延迟可测</span><span>Token 未采集则明确标记</span><span>不把内部集结果冒充线上效果</span></div><div class="module-grid">${data.scenarios.map(item => `<article class="module-card scenario-card"><div class="card-heading"><h3>${escape(item.name)}</h3>${badge(runtimeLabels[item.runtime] || item.runtime, item.runtime)}</div><ol>${item.steps.map(step => `<li>${escape(step)}</li>`).join('')}</ol><details><summary>验收条件</summary><ul>${item.assertions.map(assertion => `<li>${escape(assertion)}</li>`).join('')}</ul></details></article>`).join('')}</div>`;
  }

  async function runEvaluation() {
    loading('正在运行 100 条路由与 6 条 SQL 安全评测…');
    try {
      const data = await API.request('/evaluations/agent/run?limit=100', {method: 'POST'});
      content().innerHTML = `<div class="module-grid"><article class="module-card metric-card"><div class="module-muted">路由准确率</div><div class="metric-value">${data.routing.accuracy_pct}%</div></article><article class="module-card metric-card"><div class="module-muted">SQL 安全准确率</div><div class="metric-value">${data.sql_safety.block_or_allow_accuracy_pct}%</div></article><article class="module-card metric-card"><div class="module-muted">P95 延迟</div><div class="metric-value">${data.routing.latency_ms.p95}ms</div></article><article class="module-card metric-card"><div class="module-muted">Token</div><div class="metric-value">未测量</div></article><article class="module-card full"><h3>评测说明</h3><p>结果来自项目内固定评测集，用于回归，不代表真实生产流量。模型：${escape(data.environment?.model || '未记录')}。</p></article></div>`;
    } catch (exception) { error(exception.message); }
  }

  const loaders = {dashboard: loadDashboard, products: loadProducts, capabilities: loadCapabilities, reports: loadReports, automation: loadAutomation, data: loadData, integrations: loadIntegrations, evaluation: loadEvaluation};

  async function loadModule(name) {
    if (!moduleNames[name]) name = 'chat';
    state.module = name;
    save();
    document.querySelectorAll('.module-link').forEach(button => button.classList.toggle('active', button.dataset.module === name));
    document.getElementById('workspaceTitle').innerHTML = `<span class="title-mark"></span>${escape(moduleNames[name])}`;
    document.getElementById('workspaceSubtitle').textContent = moduleDescriptions[name];
    const isChat = name === 'chat';
    document.getElementById('chatView').hidden = !isChat;
    document.getElementById('moduleView').hidden = isChat;
    document.getElementById('sidebar').style.display = isChat ? '' : 'none';
    if (!isChat) {
      try { await loaders[name](); } catch (exception) { error(exception.message); }
    }
  }

  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.module-link').forEach(button => {
      button.setAttribute('aria-label', moduleNames[button.dataset.module]);
      button.title = moduleNames[button.dataset.module];
      button.addEventListener('click', () => loadModule(button.dataset.module));
    });
    loadModule(state.module);
  });

  window.workbench = {autoSnapshots, reload: () => loadModule(state.module), openModule: loadModule, generateReport, runWorkflow, inspectImport, confirmImport, runEvaluation};
})();
