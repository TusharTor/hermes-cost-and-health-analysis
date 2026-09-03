const state = {
  runs: [], runId: null, summary: null, resources: [], filtered: [], selectedResource: null,
  overallCostByRun: {}, selectedSeverity: 'all', healthMode: 'raw', lastHealthPayload: null,
  followLatest: true, lastLoadedAt: null, refreshTimer: null,
  selectedHealthTimestamp: null
};

const LIVE_REFRESH_MS = 60_000;
const TOR_OPS_BASE = '/tor-ops-agent/dashboard';
const APP_BASE = window.CLOUDVITALS_API_BASE || (window.location.pathname === TOR_OPS_BASE || window.location.pathname.startsWith(`${TOR_OPS_BASE}/`) ? TOR_OPS_BASE : '');
const $ = (id) => document.getElementById(id);

function aggregateOverallCost(costByResource = {}) {
  const actualTotals = new Map();
  const predictedTotals = new Map();
  Object.values(costByResource || {}).forEach(rows => {
    (rows || []).forEach(row => {
      const day = row.AnalysisDate;
      const amount = Number(isPredictedCostRow(row) ? (row.PredictedCost ?? row.CostAmount) : row.CostAmount);
      if (!day || Number.isNaN(amount)) return;
      const bucket = isPredictedCostRow(row) ? predictedTotals : actualTotals;
      bucket.set(day, (bucket.get(day) || 0) + amount);
    });
  });
  for (const day of actualTotals.keys()) predictedTotals.delete(day);
  return [
    ...[...actualTotals.entries()].map(([AnalysisDate, CostAmount]) => ({AnalysisDate, CostAmount})),
    ...[...predictedTotals.entries()].map(([AnalysisDate, CostAmount]) => ({AnalysisDate, CostAmount, PredictedCost: CostAmount, IsPredicted: true, PointType: 'Predicted'})),
  ].sort((a, b) => String(a.AnalysisDate || '').localeCompare(String(b.AnalysisDate || '')) || (isPredictedCostRow(a) ? 1 : 0) - (isPredictedCostRow(b) ? 1 : 0));
}

function healthNoDataMessage(azureRecord, mongoRecord, date) {
  const selectedDate = date || 'the selected date';
  const azureErrors = (azureRecord && azureRecord.MetricErrors) || [];
  const hasRetentionError = azureRecord && (azureRecord.TemporalCoverage === 'AzureMetricRetentionExpired' || azureErrors.some(e => String((e && e.Error) || '').includes('Max metrics retention period')));
  if (hasRetentionError) {
    return `Azure_Health_Analysis contains this resource, but ${selectedDate} is outside Azure Monitor platform metrics retention. No hourly Azure metric graph can be drawn from Azure Monitor for that historical point.`;
  }
  if (mongoRecord && mongoRecord.TemporalCoverage === 'HourlySnapshotsUnavailable') {
    return mongoRecord.CoverageNote || `Mongo_Health_Analysis contains this MongoDB resource, but no command-derived hourly snapshots matched ${selectedDate}. Values were not fabricated or backfilled.`;
  }
  const firstError = azureErrors.find(e => e && e.Error);
  if (firstError) {
    return `Azure_Health_Analysis contains this resource, but no hourly points matched ${selectedDate} because metric lookup failed: ${firstError.Error}`;
  }
  return 'No hourly health data was available for this resource/date in Azure_Health_Analysis or Mongo_Health_Analysis. Showing summary-only health status without fabricated graph points.';
}

function healthNoDataMessageFromCoverage(coverageRows = [], date) {
  const selectedDate = date || 'the selected date';
  const rows = Array.isArray(coverageRows) ? coverageRows : [];
  const azureRecord = rows.find(r => r && r.source === 'Azure_Health_Analysis');
  const mongoRecord = rows.find(r => r && r.source === 'Mongo_Health_Analysis');
  const azureErrors = (azureRecord && azureRecord.MetricErrors) || [];
  const hasRetentionError = azureRecord && (azureRecord.TemporalCoverage === 'AzureMetricRetentionExpired' || azureErrors.some(e => String((e && e.Error) || '').includes('Max metrics retention period')));
  if (hasRetentionError) {
    return `Azure_Health_Analysis contains this resource, but ${selectedDate} is outside Azure Monitor platform metrics retention. No hourly Azure metric graph can be drawn from Azure Monitor for that historical point.`;
  }
  if (mongoRecord && mongoRecord.TemporalCoverage === 'HourlySnapshotsUnavailable') {
    return mongoRecord.CoverageNote || `Mongo_Health_Analysis contains this MongoDB resource, but no command-derived hourly snapshots matched ${selectedDate}. Values were not fabricated or backfilled.`;
  }
  const firstError = azureErrors.find(e => e && e.Error);
  if (firstError) {
    return `Azure_Health_Analysis contains this resource, but no hourly points matched ${selectedDate} because metric lookup failed: ${firstError.Error}`;
  }
  if (rows.length) {
    const source = rows.map(r => r.source).filter(Boolean).join(' / ') || 'Split health analysis';
    return `${source} contains this resource, but no hourly points matched ${selectedDate}. Showing summary context without fabricated graph points.`;
  }
  return 'No hourly health data was available for this resource/date in Azure_Health_Analysis or Mongo_Health_Analysis. Showing summary-only health status without fabricated graph points.';
}

async function staticApi(path, params = {}) {
  const data = window.CLOUDVITALS_STATIC_DATA;
  const runId = params.run_id && params.run_id !== 'latest' ? params.run_id : data.latest_run_id;
  if (path === '/api/runs') return data.runs;
  if (path === '/api/summary') return data.summaries[runId];
  if (path === '/api/resources') return data.resources[runId] || [];
  if (path === '/api/cost/overall') return (data.overallCost && data.overallCost[runId]) || aggregateOverallCost(data.cost[runId] || {});
  if (path === '/api/cost') return (data.cost[runId] || {})[params.resource_id] || [];
  if (path === '/api/health') {
    const key = `${params.resource_id}|${params.date || ''}`;
    if (data.healthIndex && data.healthIndex[runId] && data.healthIndex[runId][key]) {
      const entry = data.healthIndex[runId][key];
      const res = await fetch(entry.file, {cache: 'no-store'});
      if (!res.ok) throw new Error(`Health shard failed to load: HTTP ${res.status}`);
      const shard = await res.json();
      const series = shard.series || [];
      const summary = ((data.healthSummary[runId] || {})[params.resource_id]) || null;
      return {source: shard.source || entry.source || 'Split health analysis static shard', health_kind: shard.health_kind || entry.health_kind || null, run_id: runId, ResourceID: params.resource_id, date: params.date, series, summary, message: series.length ? null : healthNoDataMessageFromCoverage(((data.healthCoverage && data.healthCoverage[runId]) || {})[params.resource_id], params.date)};
    }
    const series = ((data.healthSeries && data.healthSeries[runId] || {})[key]) || [];
    const summary = ((data.healthSummary[runId] || {})[params.resource_id]) || null;
    const hasMongo = series.some(s => ['MongoDB','MongoAtlas'].some(marker => String(s.HealthSource || '').includes(marker)) || ['StorageSize','Connections','AtlasTier','SlowQueryCount','SlowQueryNamespaces'].includes(s.MetricCategory));
    const hasAzure = series.some(s => String(s.HealthSource || '').includes('Azure') || (!['MongoDB','MongoAtlas'].some(marker => String(s.HealthSource || '').includes(marker)) && ['CPU','MemoryUsage','Disk','Network','SNAT','TrafficGiB','AvgConn','SNATPeak'].includes(s.MetricCategory)));
    const mongoRecord = ((data.mongoHealth && data.mongoHealth[runId]) || []).find(r => r.ResourceID === params.resource_id);
    const azureRecord = ((data.azureHealth && data.azureHealth[runId]) || []).find(r => r.ResourceID === params.resource_id);
    const coverageRows = ((data.healthCoverage && data.healthCoverage[runId]) || {})[params.resource_id] || [];
    const hasMongoRecord = Boolean(mongoRecord);
    const hasAzureRecord = Boolean(azureRecord);
    const hasCoverageMongo = coverageRows.some(r => r && r.source === 'Mongo_Health_Analysis');
    const hasCoverageAzure = coverageRows.some(r => r && r.source === 'Azure_Health_Analysis');
    const source = hasMongo && !hasAzure ? 'Mongo_Health_Analysis static snapshot' : (hasAzure && !hasMongo ? 'Azure_Health_Analysis static snapshot' : (hasMongoRecord || hasCoverageMongo ? 'Mongo_Health_Analysis static snapshot' : (hasAzureRecord || hasCoverageAzure ? 'Azure_Health_Analysis static snapshot' : (series.length ? 'Split health analysis static snapshot' : 'Health-Analysis summary'))));
    const health_kind = hasMongo && !hasAzure ? 'mongodb' : (hasAzure && !hasMongo ? 'azure' : (hasMongoRecord || hasCoverageMongo ? 'mongodb' : (hasAzureRecord || hasCoverageAzure ? 'azure' : (series.length ? 'mixed' : null))));
    return {source, health_kind, run_id: runId, ResourceID: params.resource_id, date: params.date, series, summary, message: series.length ? null : (coverageRows.length ? healthNoDataMessageFromCoverage(coverageRows, params.date) : healthNoDataMessage(azureRecord, mongoRecord, params.date))};
  }
  throw new Error(`Unknown static API path: ${path}`);
}
const api = async (path, params = {}) => {
  if (window.CLOUDVITALS_STATIC_DATA) return await staticApi(path, params);
  const url = new URL(`${APP_BASE}${path}`, window.location.origin);
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null) url.searchParams.set(k, v);
  const res = await fetch(url);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
};

function severityColor(sev) {
  return {Critical:'#ff4d6d', High:'#fb923c', Medium:'#fbbf24', Low:'#60a5fa', Normal:'#8a8f98'}[sev] || '#8a8f98';
}
function metricColor(i) { return ['#7170ff','#10b981','#f59e0b','#60a5fa','#ff4d6d','#a78bfa','#22d3ee'][i % 7]; }
const ACTUAL_COST_COLOR = '#7170ff';
const PREDICTED_COST_COLOR = '#22d3ee';
function isPredictedCostRow(row) {
  const marker = String((row && (row.PointType || row.CostType || row.Type)) || '').toLowerCase();
  return Boolean(row && (row.IsPredicted || row.Predicted || ['predicted', 'forecast', 'forecasted'].includes(marker)));
}
function costValue(row) {
  const value = isPredictedCostRow(row) && row.PredictedCost !== undefined ? row.PredictedCost : row.CostAmount;
  return Number(value) || 0;
}
function fmt(n, digits=2) { return n === null || n === undefined || Number.isNaN(Number(n)) ? '—' : Number(n).toLocaleString(undefined, {maximumFractionDigits: digits}); }
function shortRid(rid) { return rid && rid.includes('/') ? rid.split('/').filter(Boolean).slice(-1)[0] : rid; }
function isMongoHealthPayload(payload) {
  const series = (payload && payload.series) || [];
  return Boolean(payload && (payload.health_kind === 'mongodb' || series.some(s => ['MongoDB','MongoAtlas'].some(marker => String(s.HealthSource || '').includes(marker)) || ['StorageSize','Connections','AtlasTier','SlowQueryCount','SlowQueryNamespaces'].includes(s.MetricCategory))));
}
function isPlottableHealthSeries(series) {
  const category = String((series && series.MetricCategory) || '');
  if (['AtlasTier', 'SlowQueryCount', 'SlowQueryNamespaces'].includes(category)) return false;
  return true;
}
function mongoKpiMetaAt(payload, timestamp) {
  const matches = [];
  ((payload && payload.series) || []).forEach(s => (s.Points || []).forEach(p => {
    if (!timestamp || p.Timestamp === timestamp) matches.push({series: s, point: p});
  }));
  const summary = (payload && payload.summary) || {};
  const healthSummary = summary.HealthSummary || {};
  const point = (matches.find(m => m.point.Tier || m.point.SlowQueryCount !== undefined) || {}).point || {};
  const slowPoint = (matches.find(m => m.point.SlowQueryCount !== undefined) || {}).point || point;
  const namespaces = slowPoint.SlowQueryNamespaces || slowPoint.Namespaces || healthSummary.SlowQueryNamespaces || [];
  return {
    timestamp,
    tier: point.Tier || summary.Tier || healthSummary.LatestTier || 'Not Available',
    slowQueryCount: slowPoint.SlowQueryCount ?? healthSummary.SlowQueryCount ?? 0,
    slowQueryNamespaces: Array.isArray(namespaces) ? namespaces : [],
    storageSizeMB: point.StorageSizeMB ?? point.MemoryResidentMB ?? healthSummary.PeakStorageSizeMB ?? null,
  };
}
function renderMongoPointKpis(timestamp) {
  const container = $('mongoPointKpis');
  if (!container || !isMongoHealthPayload(state.lastHealthPayload)) return;
  if (!timestamp) {
    container.innerHTML = '<div class="health-card"><strong>MongoDB point KPIs</strong><span class="muted">Click an hourly MongoDB health point to show Tier and SlowQueryCount for that exact hour.</span></div>';
    return;
  }
  const kpi = mongoKpiMetaAt(state.lastHealthPayload, timestamp);
  const nsText = kpi.slowQueryNamespaces.length ? kpi.slowQueryNamespaces.slice(0, 6).join(', ') : 'None';
  container.innerHTML = `
    <div class="health-card"><strong>Selected Hour</strong><span class="muted mono">${esc(kpi.timestamp)}</span></div>
    <div class="health-card"><strong>Tier</strong><span class="muted">${esc(kpi.tier)}</span></div>
    <div class="health-card"><strong>SlowQuery Count</strong><span class="muted">${fmt(kpi.slowQueryCount, 0)}</span></div>
    <div class="health-card"><strong>SlowQuery Namespaces</strong><span class="muted">${esc(nsText)}</span></div>`;
}
function renderPointKpis(timestamp) {
  const container = $('healthSummary');
  if (!container || !state.lastHealthPayload) return;
  state.selectedHealthTimestamp = timestamp || null;
  // update metric cards values
  const series = (state.lastHealthPayload.series || []).filter(isPlottableHealthSeries);
  series.forEach((s, i) => {
    const valueEl = document.getElementById(`metricValue-${i}`);
    if (!valueEl) return;
    const descEl = document.getElementById(`metricDesc-${i}`);
    if (!timestamp) {
      valueEl.textContent = '';
      if (descEl && descEl.dataset && descEl.dataset.desc) descEl.textContent = descEl.dataset.desc;
      return;
    }
    // when a timestamp is selected, clear the description to avoid duplicate/leftover strings
    if (descEl && descEl.dataset) descEl.textContent = '';
    const match = (s.Points || []).find(p => p.Timestamp === timestamp);
    if (match && match.Value !== undefined && match.Value !== null && !Number.isNaN(Number(match.Value))) {
      valueEl.textContent = `${fmt(Number(match.Value))} ${s.Unit || ''}`;
    } else {
      valueEl.textContent = '—';
    }
  });
  // also render mongo extra KPIs if applicable
  if (isMongoHealthPayload(state.lastHealthPayload)) renderMongoPointKpis(timestamp);
}
function esc(s) { return String(s ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c])); }

function showEmpty(container, message) {
  container.innerHTML = `<div class="empty-state"><div class="empty-icon">⌁</div><h4>No data</h4><p>${esc(message)}</p></div>`;
}

function setOverallButtonVisible(visible) {
  const btn = $('overallBtn');
  if (!btn) return;
  btn.classList.toggle('hidden', !visible);
}

function resetHealthDrilldown(message = 'Health drilldown will appear after selecting a resource-level cost point.') {
  state.lastHealthPayload = null;
  $('healthContext').textContent = message;
  $('healthSummary').innerHTML = '';
  showEmpty($('healthChart'), message);
}

async function init() {
  $('refreshBtn').addEventListener('click', () => {
    state.followLatest = true;
    loadRuns({forceLatest: true});
  });
  $('runSelect').addEventListener('change', () => {
    state.followLatest = $('runSelect').selectedIndex === 0;
    loadRun($('runSelect').value);
  });
  $('overallBtn').addEventListener('click', () => showOverallView());
  $('resourceSearch').addEventListener('input', renderResourceList);
  document.querySelectorAll('[data-severity]').forEach(btn => btn.addEventListener('click', () => {
    document.querySelectorAll('[data-severity]').forEach(b => b.classList.remove('active'));
    btn.classList.add('active'); state.selectedSeverity = btn.dataset.severity; renderResourceList();
  }));
  $('rawMode').addEventListener('click', () => { state.healthMode='raw'; $('rawMode').classList.add('active'); $('normalizedMode').classList.remove('active'); renderHealthChart(state.lastHealthPayload); });
  $('normalizedMode').addEventListener('click', () => { state.healthMode='normalized'; $('normalizedMode').classList.add('active'); $('rawMode').classList.remove('active'); renderHealthChart(state.lastHealthPayload); });
  await loadRuns({forceLatest: true});
  state.refreshTimer = setInterval(() => loadRuns({preserveView: true}).catch(console.error), LIVE_REFRESH_MS);
}

async function loadRuns(options = {}) {
  const previousRunId = state.runId;
  state.runs = await api('/api/runs');
  const select = $('runSelect');
  select.innerHTML = state.runs.map((r, i) => `<option value="${esc(r.run_id)}">${i === 0 ? 'LIVE · ' : ''}${esc(r.fromDate || '?')} → ${esc(r.toDate || '?')} · ${esc(r.run_id)}</option>`).join('');
  if (!state.runs.length) {
    showEmpty($('costChart'), 'No Cost-Health analysis runs found in /opt/data.');
    resetHealthDrilldown('Health drilldown will appear after selecting a resource-level cost point.');
    return;
  }
  const currentStillExists = previousRunId && state.runs.some(r => r.run_id === previousRunId);
  const targetRunId = (options.forceLatest || state.followLatest || !currentStillExists) ? state.runs[0].run_id : previousRunId;
  await loadRun(targetRunId, {preserveSelection: options.preserveView && targetRunId === previousRunId});
}

async function loadRun(runId, options = {}) {
  const priorResource = options.preserveSelection ? state.selectedResource : null;
  state.runId = runId;
  state.selectedResource = null;
  state.followLatest = state.runs.length ? runId === state.runs[0].run_id : state.followLatest;
  $('runSelect').value = runId;
  const [summary, resources, overallCost] = await Promise.all([
    api('/api/summary', {run_id: runId}),
    api('/api/resources', {run_id: runId}),
    api('/api/cost/overall', {run_id: runId}).catch(err => {
      console.warn('Overall cost trend unavailable', err);
      return [];
    })
  ]);
  state.summary = summary; state.resources = resources; state.overallCostByRun[runId] = overallCost; state.lastLoadedAt = new Date();
  renderSummary(summary);
  renderResourceList();
  const resourceToSelect = priorResource && resources.some(r => r.ResourceID === priorResource) ? priorResource : null;
  if (resourceToSelect) {
    await selectResource(resourceToSelect);
  } else {
    showOverallView();
  }
}

function renderSummary(s) {
  $('runMeta').innerHTML = [
    ['Mode', state.followLatest ? 'Live latest · auto-refresh 60s' : 'Historical run'],
    ['Date range', `${s.fromDate || '?'} → ${s.toDate || '?'}`], ['Run ID', s.run_id],
    ['Hourly health', s.health_timeseries_series_count > 0 ? `${s.health_timeseries_series_count} legacy series` : (s.has_split_health_analysis ? 'Split Azure/Mongo files' : (s.has_health_timeseries ? 'Generated: 0 series' : 'Not generated'))], ['Last loaded', state.lastLoadedAt ? state.lastLoadedAt.toLocaleTimeString() : '—'], ['Data dir', '/opt/data']
  ].map(([k,v]) => `<div class="meta-item"><span class="meta-label">${k}</span><span class="meta-value ${k==='Run ID'?'mono':''}">${esc(v)}</span></div>`).join('');
  const cards = [
    ['Resources', s.resource_count], ['Affected', s.affected_resource_count], ['Cost anomalies', s.cost_anomaly_records],
    ['Azure health', s.azure_health_resource_count ?? s.health_analysis_records], ['Mongo health', s.mongo_health_resource_count ?? 0]
  ];
  $('summaryCards').innerHTML = cards.map(([k,v]) => `<div class="metric-card"><span class="metric-label">${esc(k)}</span><span class="metric-value">${fmt(v,0)}</span></div>`).join('');
}

function renderResourceList() {
  const q = $('resourceSearch').value.trim().toLowerCase();
  state.filtered = state.resources.filter(r => {
    const sevOk = state.selectedSeverity === 'all' || r.MaxSeverity === state.selectedSeverity;
    const text = `${r.ResourceID} ${r.ResourceType} ${r.MaxSeverity} ${r.CostHealthCorrelation}`.toLowerCase();
    return sevOk && (!q || text.includes(q));
  });
  $('resourceCount').textContent = `${state.filtered.length} shown of ${state.resources.length}`;
  $('resourceList').innerHTML = state.filtered.map(r => `
    <div class="resource-row ${state.selectedResource===r.ResourceID?'active':''}" data-rid="${esc(r.ResourceID)}" role="button" tabindex="0" aria-pressed="${state.selectedResource===r.ResourceID?'true':'false'}">
      <div class="resource-name">${esc(r.ResourceName)}</div>
      <div class="resource-sub">${esc(r.ResourceType)}</div>
      <div class="row-metrics">
        <span class="badge ${esc(r.MaxSeverity)}">${esc(r.MaxSeverity)}</span>
        <span class="badge">${fmt(r.PeakCost)} peak</span>
        <span class="badge">${fmt(r.AnomalyCount,0)} anomalies</span>
        <span class="badge">${esc(r.CostHealthCorrelation)}</span>
      </div>
    </div>`).join('');
  document.querySelectorAll('.resource-row').forEach(row => {
    row.addEventListener('click', () => selectResource(row.dataset.rid));
    row.addEventListener('keydown', event => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        selectResource(row.dataset.rid);
      }
    });
  });
}

function showOverallView() {
  state.selectedResource = null;
  renderResourceList();
  $('costChartTitle').textContent = 'Overall cost trend';
  $('costChartSubtitle').textContent = 'All analyzed resources';
  setOverallButtonVisible(false);
  $('costPointDetail').innerHTML = '<span class="muted">Select an aggregate cost point for date-level context, or choose a resource to inspect its individual cost trend.</span>';
  resetHealthDrilldown('Health drilldown will appear after selecting a resource-level cost point.');
  const rows = state.overallCostByRun[state.runId] || [];
  if (!rows.length) {
    const message = state.resources.length ? 'Overall cost trend is unavailable for this run.' : 'No cost trend data is available for this run.';
    showEmpty($('costChart'), message);
    $('costLegend').innerHTML = '';
    return;
  }
  renderCostChart(rows, {mode: 'overall'});
}

async function selectResource(resourceId) {
  state.selectedResource = resourceId;
  renderResourceList();
  $('costChartTitle').textContent = 'Daily cost trend';
  $('costChartSubtitle').textContent = resourceId;
  setOverallButtonVisible(true);
  $('costPointDetail').innerHTML = '<span class="muted">Click a resource-level cost point to inspect health signals for the same resource and day.</span>';
  resetHealthDrilldown('Select a cost anomaly point from the chart to investigate health signals for that resource and day.');
  try {
    const rows = await api('/api/cost', {run_id: state.runId, resource_id: resourceId});
    renderCostChart(rows, {mode: 'resource'});
  } catch (err) {
    console.warn('Resource cost trend unavailable', err);
    showEmpty($('costChart'), err.message || 'No cost rows for selected resource.');
    $('costLegend').innerHTML = '';
  }
}

function renderCostChart(rows, options = {}) {
  const mode = options.mode || (state.selectedResource ? 'resource' : 'overall');
  const el = $('costChart');
  if (!rows.length) return showEmpty(el, mode === 'overall' ? 'Overall cost trend is unavailable for this run.' : 'No cost rows for selected resource.');
  const w = Math.max(el.clientWidth || 900, 640), h = 330, m = {l:60,r:22,t:24,b:46};
  const sortedRows = [...rows].sort((a, b) => String(a.AnalysisDate || '').localeCompare(String(b.AnalysisDate || '')) || (isPredictedCostRow(a) ? 1 : 0) - (isPredictedCostRow(b) ? 1 : 0));
  const costs = sortedRows.map(costValue), minY = Math.min(0, ...costs), maxY = Math.max(...costs) || 1;
  const x = i => m.l + (sortedRows.length === 1 ? 0.5 : i/(sortedRows.length-1)) * (w-m.l-m.r);
  const y = v => h-m.b - ((v-minY)/(maxY-minY || 1)) * (h-m.t-m.b);
  const indexByRow = new Map(sortedRows.map((r, i) => [r, i]));
  const actualRows = sortedRows.filter(r => !isPredictedCostRow(r));
  const predictedRows = sortedRows.filter(isPredictedCostRow);
  const pathFor = pathRows => pathRows.map((r, i) => `${i?'L':'M'}${x(indexByRow.get(r))},${y(costValue(r))}`).join(' ');
  const actualPath = pathFor(actualRows);
  // Start the forecast segment at the final actual point so the overall cost graph remains continuous,
  // while still using a distinct color for forecast/predicted costs.
  const predictedPathRows = predictedRows.length && actualRows.length ? [actualRows[actualRows.length - 1], ...predictedRows] : predictedRows;
  const predictedPath = pathFor(predictedPathRows);
  const yTicks = [0, .25, .5, .75, 1].map(p => minY + p*(maxY-minY));
  const predictedLegend = predictedRows.length ? `<span class="legend-item"><span class="dot" style="background:${PREDICTED_COST_COLOR}"></span>Predicted cost</span>` : '';
  $('costLegend').innerHTML = mode === 'overall'
    ? `<span class="legend-item"><span class="dot" style="background:${ACTUAL_COST_COLOR}"></span>Actual aggregate CostAmount</span>${predictedLegend}`
    : ['Critical','High','Medium','Low','Normal'].map(s => `<span class="legend-item"><span class="dot" style="background:${severityColor(s)}"></span>${s}</span>`).join('') + predictedLegend;
  el.innerHTML = `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    ${yTicks.map(v => `<line class="grid-line" x1="${m.l}" y1="${y(v)}" x2="${w-m.r}" y2="${y(v)}"></line><text class="axis-text" x="8" y="${y(v)+4}">${fmt(v)}</text>`).join('')}
    <line class="axis" x1="${m.l}" y1="${h-m.b}" x2="${w-m.r}" y2="${h-m.b}"></line>
    <line class="axis" x1="${m.l}" y1="${m.t}" x2="${m.l}" y2="${h-m.b}"></line>
    ${actualPath ? `<path class="line actual-cost-line" d="${actualPath}" stroke="${ACTUAL_COST_COLOR}"></path>` : ''}
    ${predictedPath ? `<path class="line predicted-cost-line" d="${predictedPath}" stroke="${PREDICTED_COST_COLOR}"></path>` : ''}
    ${sortedRows.map((r,i) => `<circle class="point ${isPredictedCostRow(r) ? 'predicted-point' : 'actual-point'}" data-idx="${i}" cx="${x(i)}" cy="${y(costValue(r))}" r="${isPredictedCostRow(r) ? 4.5 : (mode === 'resource' && r.IsAnomaly?6:4)}" fill="${isPredictedCostRow(r) ? PREDICTED_COST_COLOR : (mode === 'overall' ? ACTUAL_COST_COLOR : severityColor(r.Severity))}"></circle>`).join('')}
    ${sortedRows.map((r,i) => (i % Math.ceil(sortedRows.length/9)===0 || i===sortedRows.length-1) ? `<text class="axis-text" x="${x(i)-18}" y="${h-16}">${esc((r.AnalysisDate||'').slice(5))}</text>` : '').join('')}
  </svg>`;
  el.querySelectorAll('.point').forEach(pt => {
    const row = sortedRows[Number(pt.dataset.idx)];
    if (isPredictedCostRow(row)) {
      pt.addEventListener('mouseenter', e => tooltip(e, `<strong>${esc(row.AnalysisDate)}</strong><br/>Predicted cost: ${fmt(row.PredictedCost ?? row.CostAmount)}<br/>${esc(row.ForecastModel || 'Forecast')}`));
      pt.addEventListener('mouseleave', hideTooltip);
      pt.addEventListener('click', () => selectPredictedCostPoint(row, mode));
    } else if (mode === 'overall') {
      pt.addEventListener('mouseenter', e => tooltip(e, `<strong>${esc(row.AnalysisDate)}</strong><br/>Overall cost: ${fmt(row.CostAmount)}<br/>All analyzed resources`));
      pt.addEventListener('mouseleave', hideTooltip);
      pt.addEventListener('click', () => selectOverallCostPoint(row));
    } else {
      pt.addEventListener('mouseenter', e => tooltip(e, `<strong>${esc(row.AnalysisDate)}</strong><br/>Cost: ${fmt(row.CostAmount)}<br/>Severity: ${esc(row.Severity)}<br/>Status: ${esc(row.TrendStatus)}<br/>Deviation: ${fmt(row.DeviationPercentage)}%`));
      pt.addEventListener('mouseleave', hideTooltip);
      pt.addEventListener('click', () => selectCostPoint(row));
    }
  });
}

function selectPredictedCostPoint(row, mode) {
  const forecastRange = row.ForecastStart && row.ForecastEnd ? ` · Forecast ${esc(row.ForecastStart)} → ${esc(row.ForecastEnd)}` : '';
  const scope = mode === 'overall' ? 'Overall predicted cost' : 'Resource predicted cost';
  $('costPointDetail').innerHTML = `<strong>${esc(row.AnalysisDate)}</strong> · ${scope} <strong style="color:${PREDICTED_COST_COLOR}">${fmt(row.PredictedCost ?? row.CostAmount)}</strong>${forecastRange}<br/><span class="muted">Predicted point shown in a different color to bifurcate forecast from actual cost. Health drilldown is available only for actual historical cost points.</span>`;
  resetHealthDrilldown('Predicted cost points do not have historical health drilldown. Select an actual resource-level cost point to inspect health signals.');
}

function selectOverallCostPoint(row) {
  $('costPointDetail').innerHTML = `<strong>${esc(row.AnalysisDate)}</strong> · Overall cost <strong>${fmt(row.CostAmount)}</strong><br/><span class="muted">Aggregate CostAmount across all analyzed resources in this run. Select a resource to inspect resource-level cost and health signals.</span>`;
  resetHealthDrilldown('Select a resource to inspect resource-level cost and health signals for a specific day.');
}

async function selectCostPoint(row) {
  const resourceId = state.selectedResource || row.ResourceID;
  if (!resourceId) return selectOverallCostPoint(row);
  $('costPointDetail').innerHTML = `<strong>${esc(row.AnalysisDate)}</strong> · Cost <strong>${fmt(row.CostAmount)}</strong> · <span style="color:${severityColor(row.Severity)}">${esc(row.Severity)}</span> · ${esc(row.TrendStatus)}<br/><span class="muted">${esc(row.AnalysisReason)}</span>`;
  $('healthContext').textContent = `${shortRid(resourceId)} · ${row.AnalysisDate} · ${row.TrendStatus}`;
  const payload = await api('/api/health', {run_id: state.runId, resource_id: resourceId, date: row.AnalysisDate});
  state.lastHealthPayload = payload;
  renderHealthChart(payload);
  renderHealthSummary(payload);
}

function renderHealthChart(payload) {
  const el = $('healthChart');
  if (!payload) return showEmpty(el, 'Health drilldown will appear after selecting a resource-level cost point.');
  const series = (payload.series || []).filter(isPlottableHealthSeries);
  if (!series.length) return showEmpty(el, payload.message || 'No hourly health series available for this point.');
  const normalized = state.healthMode === 'normalized';
  const w = Math.max(el.clientWidth || 900, 640), h = 300, m = {l:58,r:24,t:24,b:48};
  const flat = [];
  series.forEach((s, si) => (s.Points||[]).forEach((p, pi) => flat.push({s, si, pi, point:p, t:p.Timestamp, v:Number(p.Value)})));
  if (!flat.length) return showEmpty(el, 'Health time-series contained no points.');
  const times = [...new Set(flat.map(p => p.t))].sort();
  const byTime = new Map(times.map((t,i)=>[t,i]));
  const valuesFor = s => (s.Points||[]).map(p => Number(p.Value)).filter(v => !Number.isNaN(v));
  const plotVal = (s, v) => {
    if (!normalized) return v;
    const vals = valuesFor(s), min = Math.min(...vals), max = Math.max(...vals);
    return max === min ? 50 : (v-min)/(max-min)*100;
  };
  const plotted = flat.map(p => plotVal(p.s, p.v));
  const minY = Math.min(0, ...plotted), maxY = Math.max(...plotted) || 1;
  const x = t => m.l + (times.length===1 ? .5 : byTime.get(t)/(times.length-1))*(w-m.l-m.r);
  const y = v => h-m.b - ((v-minY)/(maxY-minY || 1))*(h-m.t-m.b);
  const yTicks = [0,.25,.5,.75,1].map(p => minY + p*(maxY-minY));
  const paths = series.map((s, si) => {
    const pts = (s.Points||[]).filter(p => !Number.isNaN(Number(p.Value))).map((p,i) => `${i?'L':'M'}${x(p.Timestamp)},${y(plotVal(s, Number(p.Value)))}`).join(' ');
    return `<path class="line" d="${pts}" stroke="${metricColor(si)}"></path>${(s.Points||[]).map((p, pi) => `<circle class="point" data-si="${si}" data-pi="${pi}" cx="${x(p.Timestamp)}" cy="${y(plotVal(s, Number(p.Value)))}" r="4" fill="${metricColor(si)}"></circle>`).join('')}`;
  }).join('');
  el.innerHTML = `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    ${yTicks.map(v => `<line class="grid-line" x1="${m.l}" y1="${y(v)}" x2="${w-m.r}" y2="${y(v)}"></line><text class="axis-text" x="8" y="${y(v)+4}">${fmt(v)}</text>`).join('')}
    <line class="axis" x1="${m.l}" y1="${h-m.b}" x2="${w-m.r}" y2="${h-m.b}"></line><line class="axis" x1="${m.l}" y1="${m.t}" x2="${m.l}" y2="${h-m.b}"></line>
    ${paths}
    ${times.map((t,i) => (i % Math.ceil(times.length/8)===0 || i===times.length-1) ? `<text class="axis-text" x="${x(t)-18}" y="${h-16}">${esc(t.slice(11,16))}</text>` : '').join('')}
  </svg>`;
  el.querySelectorAll('.point').forEach(pt => {
    const s = series[Number(pt.dataset.si)];
    const p = s && (s.Points || [])[Number(pt.dataset.pi)];
    if (!s || !p) return;
    pt.addEventListener('mouseenter', e => tooltip(e, `<strong>${esc(p.Timestamp)}</strong><br/>${esc(s.MetricCategory || s.MetricName)}: ${fmt(p.Value)} ${esc(s.Unit || '')}`));
    pt.addEventListener('mouseleave', hideTooltip);
    pt.addEventListener('click', () => renderPointKpis(p.Timestamp));
  });
  // restore previously-selected timestamp values if any
  renderPointKpis(state.selectedHealthTimestamp);
}

function renderHealthSummary(payload) {
  const s = payload.summary || {};
  const series = (payload.series || []).filter(isPlottableHealthSeries);
  const metricCards = series.length ? series.map((m,i) => {
    const desc = `${esc(m.MetricName)} · ${esc(m.Unit || 'unit')} · ${(m.Points||[]).length} pts`;
    return `
    <div class="health-card"><strong style="color:${metricColor(i)}">${esc(m.MetricCategory || m.MetricName)}</strong>
      <span class="muted" id="metricDesc-${i}" data-desc="${esc(desc)}">${esc(desc)}</span>
      <span class="muted mono" id="metricValue-${i}"></span>
    </div>`;
  }).join('') : '';
  const mongoPointKpis = isMongoHealthPayload(payload) ? '<div id="mongoPointKpis" class="mongo-point-kpis" style="grid-column:1/-1"><div class="health-card"><strong>MongoDB point KPIs</strong><span class="muted">Click an hourly MongoDB health point to show Tier and SlowQueryCount for that exact hour.</span></div></div>' : '';
  $('healthSummary').innerHTML = `
    <div class="health-card"><strong>Correlation</strong><span class="muted">${esc(s.CostHealthCorrelation || 'Not Available')}</span></div>
    <div class="health-card"><strong>Overall health</strong><span class="muted">${esc(s.OverallHealthStatus || 'Not Available')}</span></div>
    <div class="health-card"><strong>Source</strong><span class="muted">${esc(payload.source)}</span></div>
    ${metricCards}
    ${mongoPointKpis}
    <div class="health-card" style="grid-column:1/-1"><strong>Reason</strong><span class="muted">${esc(s.HealthAnalysisReason || payload.message || 'No health summary available.')}</span></div>`;
  // populate any selected timestamp values
  renderPointKpis(state.selectedHealthTimestamp);
}

let tip;
function tooltip(e, html) {
  hideTooltip(); tip = document.createElement('div'); tip.className = 'tooltip'; tip.innerHTML = html; document.body.appendChild(tip);
  tip.style.left = `${Math.min(e.clientX + 14, window.innerWidth - 380)}px`; tip.style.top = `${e.clientY + 14}px`;
}
function hideTooltip() { if (tip) tip.remove(); tip = null; }

init().catch(err => {
  console.error(err);
  showEmpty($('costChart'), err.message);
});
