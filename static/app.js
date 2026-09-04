const state = {
  runs: [], runId: null, summary: null, resources: [], filtered: [], selectedResource: null,
  overallCostByRun: {}, currentCostRows: [], selectedCostPoint: null, selectedSeverity: 'all', healthMode: 'raw', lastHealthPayload: null,
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
      return {source: shard.source || entry.source || 'Split health analysis static shard', health_kind: shard.health_kind || entry.health_kind || null, run_id: runId, ResourceID: params.resource_id, date: params.date, granularity: params.granularity || (params.date ? 'PT1H' : 'PT6H'), series, summary, message: series.length ? null : healthNoDataMessageFromCoverage(((data.healthCoverage && data.healthCoverage[runId]) || {})[params.resource_id], params.date)};
    }
    const series = ((data.healthSeries && data.healthSeries[runId] || {})[key]) || [];
    const summary = ((data.healthSummary[runId] || {})[params.resource_id]) || null;
    const hasMongo = series.some(s => ['MongoDB','MongoAtlas'].some(marker => String(s.HealthSource || '').includes(marker)) || ['StorageSize','Connections','AtlasTier','SlowQueryCount','SlowQueryNamespaces'].includes(s.MetricCategory));
    const hasAzure = series.some(s => String(s.HealthSource || '').includes('Azure') || (!['MongoDB','MongoAtlas'].some(marker => String(s.HealthSource || '').includes(marker)) && ['CPU','MemoryUsage','Disk','IOPs','Network','SNAT','TrafficGiB','AvgConn','SNATPeak'].includes(s.MetricCategory)));
    const mongoRecord = ((data.mongoHealth && data.mongoHealth[runId]) || []).find(r => r.ResourceID === params.resource_id);
    const azureRecord = ((data.azureHealth && data.azureHealth[runId]) || []).find(r => r.ResourceID === params.resource_id);
    const coverageRows = ((data.healthCoverage && data.healthCoverage[runId]) || {})[params.resource_id] || [];
    const hasMongoRecord = Boolean(mongoRecord);
    const hasAzureRecord = Boolean(azureRecord);
    const hasCoverageMongo = coverageRows.some(r => r && r.source === 'Mongo_Health_Analysis');
    const hasCoverageAzure = coverageRows.some(r => r && r.source === 'Azure_Health_Analysis');
    const source = hasMongo && !hasAzure ? 'Mongo_Health_Analysis static snapshot' : (hasAzure && !hasMongo ? 'Azure_Health_Analysis static snapshot' : (hasMongoRecord || hasCoverageMongo ? 'Mongo_Health_Analysis static snapshot' : (hasAzureRecord || hasCoverageAzure ? 'Azure_Health_Analysis static snapshot' : (series.length ? 'Split health analysis static snapshot' : 'Health-Analysis summary'))));
    const health_kind = hasMongo && !hasAzure ? 'mongodb' : (hasAzure && !hasMongo ? 'azure' : (hasMongoRecord || hasCoverageMongo ? 'mongodb' : (hasAzureRecord || hasCoverageAzure ? 'azure' : (series.length ? 'mixed' : null))));
    return {source, health_kind, run_id: runId, ResourceID: params.resource_id, date: params.date, granularity: params.granularity || (params.date ? 'PT1H' : 'PT6H'), series, summary, message: series.length ? null : (coverageRows.length ? healthNoDataMessageFromCoverage(coverageRows, params.date) : healthNoDataMessage(azureRecord, mongoRecord, params.date))};
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

const SEVERITY_RANK = {Normal:0, Low:1, Medium:2, High:3, Critical:4};
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
function isAzureHealthPayload(payload) {
  if (!payload || payload.health_kind === 'mongodb' || isMongoHealthPayload(payload)) return false;
  const series = (payload && payload.series) || [];
  return Boolean(payload && (payload.health_kind === 'azure' || series.some(s => String(s.HealthSource || '').includes('Azure') || ['CPU','Disk','IOPs','Network','SNAT','TrafficGiB','AvgConn','SNATPeak'].includes(s.MetricCategory))));
}
function isPlottableHealthSeries(series) {
  const category = String((series && series.MetricCategory) || '');
  if (['AtlasTier', 'SlowQueryCount', 'SlowQueryNamespaces'].includes(category)) return false;
  return true;
}
function isAzurePercentSeries(series) {
  const unit = String((series && series.Unit) || '').toLowerCase();
  const name = `${(series && series.MetricName) || ''} ${(series && series.MetricCategory) || ''}`.toLowerCase();
  if (unit === 'percent' || unit === '%' || name.includes('percent') || name.includes('percentage')) return true;
  const vals = ((series && series.Points) || []).map(p => numOrNull(p.Value)).filter(v => v !== null);
  return vals.length > 0 && vals.every(v => v >= 0 && v <= 100) && !['bytes', 'seconds', 'milliseconds'].includes(unit);
}
function hasNumericHealthPoints(series) {
  return ((series && series.Points) || []).some(p => numOrNull(p.Value) !== null);
}
function hasAzurePercentGraph(payload) {
  if (!isAzureHealthPayload(payload)) return false;
  return ((payload && payload.series) || []).some(s => {
    const category = String((s && s.MetricCategory) || '');
    return isPlottableHealthSeries(s) && ['CPU', 'MemoryUsage'].includes(category) && isAzurePercentSeries(s) && hasNumericHealthPoints(s);
  });
}
function isGraphHealthSeries(series, payload) {
  if (!isPlottableHealthSeries(series)) return false;
  const category = String((series && series.MetricCategory) || '');
  if (isMongoHealthPayload(payload)) return ['Connections', 'MemoryUsage', 'StorageSize'].includes(category);
  if (isAzureHealthPayload(payload)) {
    if (['Disk', 'IOPs'].includes(category)) return false;
    if (hasAzurePercentGraph(payload)) return ['CPU', 'MemoryUsage'].includes(category) && isAzurePercentSeries(series);
    return hasNumericHealthPoints(series);
  }
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
  const scriptPoint = (matches.find(m => m.point.ScriptScheduleContext || m.point.ScheduledScripts) || {}).point || {};
  const scriptContext = scriptPoint.ScriptScheduleContext || (scriptPoint.ScheduledScripts ? {scheduled_scripts: scriptPoint.ScheduledScripts, note: 'MachineData scripts scheduled for the selected MongoDB health hour. Supporting context only; no causality is inferred.'} : null);
  const namespaces = slowPoint.SlowQueryNamespaces || slowPoint.Namespaces || healthSummary.SlowQueryNamespaces || [];
  return {
    timestamp,
    tier: point.Tier || summary.Tier || healthSummary.LatestTier || 'Not Available',
    slowQueryCount: slowPoint.SlowQueryCount ?? healthSummary.SlowQueryCount ?? 0,
    slowQueryNamespaces: Array.isArray(namespaces) ? namespaces : [],
    storageSizeMB: point.StorageSizeMB ?? point.MemoryResidentMB ?? healthSummary.PeakStorageSizeMB ?? null,
    scriptContext,
  };
}
function scriptScheduleTooltip(point) {
  const context = point && (point.ScriptScheduleContext || (point.ScheduledScripts ? {scheduled_scripts: point.ScheduledScripts} : null));
  if (!context) return '';
  const scheduled = context.scheduled_scripts || [];
  if (!scheduled.length) return '<br/>Scheduled Python Scripts: none in this selected hour';
  const shown = scheduled.slice(0, 4).map(s => {
    const slots = s.scheduled_slots && s.scheduled_slots.length ? ` · slot ${s.scheduled_slots.join('/')}` : '';
    return esc(`${s.script_name}${slots}`);
  }).join('<br/>');
  return `<br/>Scheduled Python Scripts:<br/>${shown}${scheduled.length > 4 ? '<br/>…' : ''}`;
}
function graphUnavailableMessage(payload) {
  const allSeries = (payload && payload.series) || [];
  if (!allSeries.length) return (payload && payload.message) || 'No hourly health series available for this point.';
  const available = allSeries.map(s => `${s.MetricCategory || s.MetricName || 'metric'}${s.Unit ? ` (${s.Unit})` : ''}`).join(', ');
  if (isAzureHealthPayload(payload)) {
    const contextOnly = allSeries.filter(s => ['Disk', 'IOPs'].includes(String(s.MetricCategory || ''))).map(s => s.MetricCategory);
    const contextOnlyNote = contextOnly.length ? ` Azure ${[...new Set(contextOnly)].join('/')} metrics are retained as context only and are not plotted.` : '';
    return `${payload.message ? `${payload.message} ` : ''}No Azure health graph was drawn because none of the available non-disk/non-IOPs Azure health series contains numeric points. Available context-only metrics for this selection: ${available}.${contextOnlyNote} No percentage values were fabricated.`;
  }
  if (isMongoHealthPayload(payload)) {
    return `${payload.message ? `${payload.message} ` : ''}No MongoDB health graph was drawn because no Connections, MemoryUsage, or StorageSize time-series points were available for this selection. Available context-only metrics: ${available}.`;
  }
  return payload.message || `No graph-compatible health series was available for this selection. Available context-only metrics: ${available}.`;
}
function renderScriptScheduleCard(scriptContext) {
  if (!scriptContext) {
    return '<div class="health-card" style="grid-column:1/-1"><strong>Scheduled Python Scripts</strong><span class="muted">No MachineData script schedule mapping is configured for this MongoDB resource.</span></div>';
  }
  const scheduled = scriptContext.scheduled_scripts || [];
  const scheduledHtml = scheduled.length
    ? scheduled.map(s => `<li><span class="mono">${esc(s.script_name)}</span> — ${esc(s.scheduled_time)}${s.scheduled_slots && s.scheduled_slots.length ? ` · slot ${esc(s.scheduled_slots.join(', '))}` : ''}</li>`).join('')
    : '<li>No configured MachineData scripts are scheduled in this selected hour.</li>';
  return `
    <div class="health-card" style="grid-column:1/-1"><strong>Scheduled Python Scripts</strong><span class="muted"><ul>${scheduledHtml}</ul></span><span class="muted">${esc(scriptContext.note || 'Supporting context only; no causality is inferred.')}</span></div>`;
}
function numOrNull(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}
function selectedResourceMeta() {
  return (state.resources || []).find(r => r.ResourceID === (state.selectedResource || (state.selectedCostPoint && state.selectedCostPoint.ResourceID))) || {};
}
function hourLabel(timestamp) {
  return timestamp ? timestamp.slice(11, 16) : '';
}
function healthAxisLabel(timestamp, times = []) {
  if (!timestamp) return '';
  const uniqueDays = new Set((times || []).map(t => String(t).slice(0, 10)));
  return uniqueDays.size > 1 ? `${timestamp.slice(5, 10)} ${timestamp.slice(11, 16)}` : timestamp.slice(11, 16);
}
function healthSeriesPointStats(series, timestamp) {
  const points = (series && series.Points) || [];
  const values = points.map(p => numOrNull(p.Value)).filter(v => v !== null);
  const match = timestamp ? points.find(p => p.Timestamp === timestamp) : null;
  const value = match ? numOrNull(match.Value) : null;
  if (!values.length) return null;
  let minPoint = null, maxPoint = null;
  points.forEach(p => {
    const v = numOrNull(p.Value);
    if (v === null) return;
    if (!minPoint || v < numOrNull(minPoint.Value)) minPoint = p;
    if (!maxPoint || v > numOrNull(maxPoint.Value)) maxPoint = p;
  });
  return {series, point: match, value, min: Math.min(...values), max: Math.max(...values), minPoint, maxPoint, count: values.length};
}
function selectedHealthStats(timestamp) {
  if (!state.lastHealthPayload) return [];
  return ((state.lastHealthPayload.series || [])
    .filter(s => isGraphHealthSeries(s, state.lastHealthPayload))
    .map(s => healthSeriesPointStats(s, timestamp))
    .filter(Boolean));
}
function metricLabel(stat) {
  const name = stat.series.MetricCategory || stat.series.MetricName || 'metric';
  const ts = (stat.maxPoint || {}).Timestamp;
  const isOverviewBucket = stat.maxPoint && ['PT24H', 'PT6H'].includes(stat.maxPoint.Granularity);
  const unit = stat.series.Unit ? ` ${esc(stat.series.Unit)}` : '';
  if (isOverviewBucket) {
    const bucketMax = numOrNull(stat.maxPoint.BucketMax ?? stat.maxPoint.DailyMax);
    const peakTs = stat.maxPoint.PeakTimestamp;
    const extra = bucketMax !== null ? `; bucket max ${fmt(bucketMax)}${unit}${peakTs ? ` at ${esc(peakTs.slice(5, 16).replace('T', ' '))}` : ''}` : '';
    return `${esc(name)} ${esc(stat.maxPoint.Granularity || 'overview')} avg peak ${fmt(stat.max)}${unit} at ${esc(ts ? ts.slice(5, 16).replace('T', ' ') : '')}${extra}`;
  }
  return `${esc(name)} ${fmt(stat.max)}${unit} at ${esc(ts ? ts.slice(5, 16).replace('T', ' ') : '')}`;
}
function metricAverage(points = []) {
  const vals = points.map(p => numOrNull(p.Value)).filter(v => v !== null);
  return vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : null;
}
function datesBetween(a, b) {
  const da = new Date(`${a}T00:00:00Z`), db = new Date(`${b}T00:00:00Z`);
  return Math.round((db - da) / 86400000);
}
function recurringHealthPatterns(payload) {
  const wanted = isAzureHealthPayload(payload)
    ? ['CPU', 'MemoryUsage', 'Network', 'SNAT', 'TrafficGiB', 'AvgConn', 'SNATPeak']
    : ['CPU', 'MemoryUsage', 'Network', 'SNAT', 'TrafficGiB', 'AvgConn', 'SNATPeak', 'Connections', 'StorageSize'];
  const patterns = [];
  ((payload && payload.series) || []).filter(s => isGraphHealthSeries(s, payload)).forEach(series => {
    const category = series.MetricCategory || series.MetricName || 'metric';
    if (!wanted.includes(category)) return;
    const pts = (series.Points || []).filter(p => numOrNull(p.DailyMax ?? p.Value) !== null && p.Timestamp);
    if (pts.length < 6) return;
    const vals = pts.map(p => numOrNull(p.DailyMax ?? p.Value));
    const min = Math.min(...vals), max = Math.max(...vals);
    if (!Number.isFinite(min) || !Number.isFinite(max) || max === min) return;
    const threshold = min + (max - min) * 0.75;
    const dayMax = new Map();
    pts.forEach(p => {
      const day = String(p.Timestamp).slice(0, 10);
      const v = numOrNull(p.DailyMax ?? p.Value);
      const existing = dayMax.get(day);
      if (!existing || v > existing.value) dayMax.set(day, {day, value: v, timestamp: p.Timestamp});
    });
    const elevated = [...dayMax.values()].filter(d => d.value >= threshold).sort((a, b) => a.day.localeCompare(b.day));
    if (elevated.length < 3) return;
    const intervals = [];
    for (let i = 1; i < elevated.length; i++) intervals.push(datesBetween(elevated[i - 1].day, elevated[i].day));
    const usable = intervals.filter(v => v >= 2 && v <= 10);
    if (usable.length < 2) return;
    const sorted = [...usable].sort((a, b) => a - b);
    const median = sorted[Math.floor(sorted.length / 2)];
    const close = usable.filter(v => Math.abs(v - median) <= 1).length;
    if (close < Math.max(2, Math.ceil(usable.length * 0.6))) return;
    patterns.push({category, interval: median, days: elevated.map(d => d.day), peak: max, unit: series.Unit || '', sample: elevated.slice(0, 4)});
  });
  return patterns.sort((a, b) => a.interval - b.interval || a.category.localeCompare(b.category));
}
function resourceCostOverview() {
  const rows = (state.currentCostRows || []).filter(r => !isPredictedCostRow(r));
  if (!rows.length) return null;
  const anomalies = rows.filter(r => r.IsAnomaly);
  const peak = [...rows].sort((a, b) => costValue(b) - costValue(a))[0];
  const maxSeverity = rows.map(r => r.Severity || 'Normal').sort((a, b) => (SEVERITY_RANK[b] || 0) - (SEVERITY_RANK[a] || 0))[0] || 'Normal';
  return {rows, anomalies, peak, maxSeverity};
}
function buildResourceOverviewInsights() {
  if (!state.selectedResource) return [];
  const insights = [];
  const meta = selectedResourceMeta();
  const payload = state.lastHealthPayload;
  const resourceName = shortRid(state.selectedResource);
  const costOverview = resourceCostOverview();
  if (costOverview && costOverview.peak) {
    insights.push(`${esc(resourceName)} (${esc(meta.ResourceType || 'Unknown type')}) has ${fmt(costOverview.anomalies.length, 0)} cost anomaly point(s); peak CostAmount is ${fmt(costValue(costOverview.peak))} on ${esc(costOverview.peak.AnalysisDate || 'unknown date')} with max severity ${esc(costOverview.maxSeverity)}.`);
  }
  const seriesStats = selectedHealthStats(null);
  if (payload && seriesStats.length) {
    const preferred = ['CPU', 'MemoryUsage', 'Network', 'SNAT', 'TrafficGiB', 'AvgConn', 'SNATPeak', 'Connections', 'StorageSize'];
    const ranked = [...seriesStats].sort((a, b) => (preferred.indexOf(a.series.MetricCategory) === -1 ? 999 : preferred.indexOf(a.series.MetricCategory)) - (preferred.indexOf(b.series.MetricCategory) === -1 ? 999 : preferred.indexOf(b.series.MetricCategory)));
    insights.push(`Resource health overview is loaded for ${esc(resourceName)} using PT6H overview points: ${ranked.slice(0, 3).map(metricLabel).join('; ')}.`);
  } else if (payload && payload.message) {
    insights.push(`Resource health overview for ${esc(resourceName)} has no plottable PT6H series: ${esc(payload.message)}`);
  }
  const patterns = recurringHealthPatterns(payload);
  if (patterns.length) {
    const top = patterns.slice(0, 3);
    const interval = top[0].interval;
    const metrics = top.map(p => p.category).join(', ');
    const days = top[0].days.slice(0, 4).join(', ');
    insights.push(`Recurring health pattern: ${esc(metrics)} reaches elevated daily peaks about every ${fmt(interval, 0)} days (${esc(days)}${top[0].days.length > 4 ? ', …' : ''}). Compare these dates with workload, backup, scaling, or deployment calendars before changing capacity.`);
  }
  return insights.slice(0, 5);
}
function healthStatusSummary(summary) {
  const statuses = [];
  [['CPU', summary.CPUStatus], ['Memory', summary.MemoryStatus], ['Disk', summary.DiskStatus], ['Network', summary.NetworkStatus]].forEach(([label, value]) => {
    if (value && value !== 'Not Available') statuses.push(`${label}: ${value}`);
  });
  return statuses;
}
function buildSuggestedInsights() {
  const row = state.selectedCostPoint;
  if (!row) return [];
  const insights = [];
  const meta = selectedResourceMeta();
  const resourceId = state.selectedResource || row.ResourceID || meta.ResourceID || 'selected resource';
  const resourceName = shortRid(resourceId);
  const resourceType = row.ResourceType || meta.ResourceType || 'Unknown type';
  const cost = numOrNull(row.CostAmount);
  const previous = numOrNull(row.PreviousCost);
  const expected = numOrNull(row.ExpectedCost ?? row.AverageCost);
  const pct = numOrNull(row.PercentageChange ?? row.DayOverDayChange ?? row.DeviationPercentage);
  const status = row.TrendStatus || row.Trend || 'Cost anomaly';
  const date = row.AnalysisDate || 'selected date';
  const direction = pct !== null ? (pct < 0 ? 'decrease' : pct > 0 ? 'increase' : 'flat change') : String(status).toLowerCase();
  if (cost !== null) {
    const prevText = previous !== null ? ` vs ${fmt(previous)} previous` : '';
    const pctText = pct !== null ? ` (${fmt(pct)}%)` : '';
    insights.push(`${esc(resourceName)} (${esc(resourceType)}) shows a ${esc(direction)} on ${esc(date)}: CostAmount ${fmt(cost)}${prevText}${pctText}, severity ${esc(row.Severity || meta.MaxSeverity || 'Not Available')}.`);
  }
  if (expected !== null && cost !== null && expected > 0 && Math.abs(cost - expected) > 0.01) {
    const delta = ((cost - expected) / expected) * 100;
    insights.push(`For this point, CostAmount is ${fmt(Math.abs(delta))}% ${delta >= 0 ? 'above' : 'below'} the available baseline/expected value ${fmt(expected)}.`);
  }
  if (row.AnalysisReason) {
    insights.push(`Cost analyzer note for this resource/date: ${esc(row.AnalysisReason)}`);
  }
  const payload = state.lastHealthPayload;
  const summary = (payload && payload.summary) || {};
  const isMongo = isMongoHealthPayload(payload);
  const seriesStats = selectedHealthStats(state.selectedHealthTimestamp);
  const allDayStats = selectedHealthStats(null);
  if (payload && !((payload.series || []).filter(isPlottableHealthSeries).length) && payload.message) {
    insights.push(`Health drilldown has no hourly series for ${esc(resourceName)} on ${esc(date)}: ${esc(payload.message)}`);
  }
  if (payload && (payload.series || []).length) {
    const statuses = healthStatusSummary(summary);
    const correlation = summary.CostHealthCorrelation || summary.CostMongoDBCorrelation;
    if (correlation && correlation !== 'Not Available') {
      insights.push(`${isMongo ? 'MongoDB' : 'Azure'} health correlation is “${esc(correlation)}”${statuses.length ? ` with ${esc(statuses.join(', '))}` : ''}; this is evidence context, not a cause statement.`);
    }
  }
  if (state.selectedHealthTimestamp && seriesStats.length) {
    const clicked = seriesStats.slice(0, 4).map(stat => {
      const name = stat.series.MetricCategory || stat.series.MetricName || 'metric';
      return `${name} ${fmt(stat.value)}${stat.series.Unit ? ` ${stat.series.Unit}` : ''}`;
    }).join(', ');
    insights.push(`Clicked ${isMongo ? 'MongoDB' : 'Azure'} health hour ${esc(hourLabel(state.selectedHealthTimestamp))}: ${esc(clicked)} for ${esc(resourceName)}.`);
  } else if (allDayStats.length) {
    const preferred = isMongo ? ['Connections', 'MemoryUsage', 'StorageSize'] : ['CPU', 'MemoryUsage', 'TrafficGiB', 'AvgConn', 'SNATPeak', 'Network', 'SNAT'];
    const rankFor = stat => {
      const idx = preferred.indexOf(stat.series.MetricCategory);
      return idx === -1 ? 999 : idx;
    };
    const ordered = [...allDayStats].sort((a, b) => rankFor(a) - rankFor(b));
    const signals = ordered.slice(0, 3).map(metricLabel).join('; ');
    insights.push(`Same-day ${isMongo ? 'MongoDB' : 'Azure'} health context for ${esc(resourceName)}: ${signals}.`);
  }
  if (isMongo && state.selectedHealthTimestamp) {
    const kpi = mongoKpiMetaAt(payload, state.selectedHealthTimestamp);
    if (kpi && numOrNull(kpi.slowQueryCount) > 0) {
      const ns = (kpi.slowQueryNamespaces || []).slice(0, 2).join(', ');
      insights.push(`At ${esc(hourLabel(state.selectedHealthTimestamp))}, SlowQueryCount is ${fmt(kpi.slowQueryCount, 0)}${ns ? ` for ${esc(ns)}` : ''}; use the namespace list to narrow the DB-side check.`);
    }
    if (kpi && kpi.scriptContext && (kpi.scriptContext.scheduled_scripts || []).length) {
      const scripts = kpi.scriptContext.scheduled_scripts.slice(0, 5).map(s => `${s.script_name}${s.scheduled_slots && s.scheduled_slots.length ? ` @ ${s.scheduled_slots.join('/')}` : ''}`).join(', ');
      insights.push(`In the same MongoDB health hour, the MachineData schedule files list ${(kpi.scriptContext.scheduled_scripts || []).length} script(s): ${esc(scripts)}${kpi.scriptContext.scheduled_scripts.length > 5 ? ', …' : ''}. Schedule context only; it does not prove DB access or causality.`);
    }
  }
  return insights.slice(0, 5);
}
function renderSuggestedInsights() {
  const container = $('insightsList');
  if (!container) return;
  const insights = state.selectedCostPoint ? buildSuggestedInsights() : buildResourceOverviewInsights();
  if (!state.selectedCostPoint && !state.selectedResource) {
    container.innerHTML = '<li class="muted">Select a resource to show cost/health overview insights, or click a resource-level cost anomaly for same-day drilldown insights.</li>';
    return;
  }
  if (!insights.length) {
    container.innerHTML = '<li class="muted">No specific, data-supported suggestions are available for the current selection.</li>';
    return;
  }
  container.innerHTML = insights.map(item => `<li>${item}</li>`).join('');
}
function renderMongoPointKpis(timestamp) {
  const container = $('mongoPointKpis');
  if (!container || !isMongoHealthPayload(state.lastHealthPayload)) return;
  if (!timestamp) {
    container.innerHTML = '<div class="health-card"><strong>MongoDB point KPIs</strong><span class="muted">Click an hourly MongoDB health point to show Tier, SlowQueryCount, and scheduled MachineData scripts for that exact hour.</span></div>';
    return;
  }
  const kpi = mongoKpiMetaAt(state.lastHealthPayload, timestamp);
  const nsText = kpi.slowQueryNamespaces.length ? kpi.slowQueryNamespaces.slice(0, 6).join(', ') : 'None';
  container.innerHTML = `
    <div class="health-card"><strong>Selected Hour</strong><span class="muted mono">${esc(kpi.timestamp)}</span></div>
    <div class="health-card"><strong>Tier</strong><span class="muted">${esc(kpi.tier)}</span></div>
    <div class="health-card"><strong>SlowQuery Count</strong><span class="muted">${fmt(kpi.slowQueryCount, 0)}</span></div>
    <div class="health-card"><strong>SlowQuery Namespaces</strong><span class="muted">${esc(nsText)}</span></div>
    ${renderScriptScheduleCard(kpi.scriptContext)}`;
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
      if (['PT24H', 'PT6H'].includes(match.Granularity) && (match.BucketMax !== undefined || match.DailyMax !== undefined)) {
        const bucketMax = match.BucketMax ?? match.DailyMax;
        valueEl.textContent = `avg ${fmt(Number(match.Value))} ${s.Unit || ''} · bucket max ${fmt(bucketMax)} ${s.Unit || ''}`;
      } else {
        valueEl.textContent = `${fmt(Number(match.Value))} ${s.Unit || ''}`;
      }
    } else {
      valueEl.textContent = '—';
    }
  });
  // also render mongo extra KPIs if applicable
  if (isMongoHealthPayload(state.lastHealthPayload)) renderMongoPointKpis(timestamp);
  renderSuggestedInsights();
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
  state.selectedHealthTimestamp = null;
  $('healthContext').textContent = message;
  $('healthSummary').innerHTML = '';
  showEmpty($('healthChart'), message);
  renderSuggestedInsights();
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

function actualTotalCostForRun(runId, summary = {}) {
  const summaryValue = summary.total_cost ?? summary.TotalCost ?? summary.totalCost ?? summary.actual_total_cost ?? summary.ActualTotalCost ?? summary.total_cost_amount ?? summary.TotalCostAmount;
  const parsedSummaryValue = numOrNull(summaryValue);
  if (parsedSummaryValue !== null) return parsedSummaryValue;
  const rows = state.overallCostByRun[runId] || [];
  return rows.reduce((total, row) => {
    if (isPredictedCostRow(row)) return total;
    return total + costValue(row);
  }, 0);
}

function renderSummary(s) {
  $('runMeta').innerHTML = [
    ['Date range', `${s.fromDate || '?'} → ${s.toDate || '?'}`],
    ['Last loaded', state.lastLoadedAt ? state.lastLoadedAt.toLocaleTimeString() : '—']
  ].map(([k,v]) => `<div class="meta-item"><span class="meta-label">${k}</span><span class="meta-value">${esc(v)}</span></div>`).join('');
  const cards = [
    ['Resources', s.resource_count, 0], ['Total cost', actualTotalCostForRun(state.runId, s), 2],
    ['Azure health', s.azure_health_resource_count ?? s.health_analysis_records, 0], ['Mongo health', s.mongo_health_resource_count ?? 0, 0]
  ];
  $('summaryCards').innerHTML = cards.map(([k,v,digits]) => `<div class="metric-card"><span class="metric-label">${esc(k)}</span><span class="metric-value">${fmt(v,digits)}</span></div>`).join('');
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
  state.selectedCostPoint = null;
  state.currentCostRows = [];
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

async function loadResourceHealthOverview(resourceId) {
  state.selectedHealthTimestamp = null;
  $('healthContext').textContent = `${shortRid(resourceId)} · resource health overview · PT6H points across available run range`;
  try {
    const payload = await api('/api/health', {run_id: state.runId, resource_id: resourceId, granularity: 'PT6H'});
    state.lastHealthPayload = payload;
    renderHealthChart(payload);
    renderHealthSummary(payload);
  } catch (err) {
    console.warn('Resource health overview unavailable', err);
    resetHealthDrilldown(err.message || 'Resource health overview is unavailable for this resource.');
  }
}

async function selectResource(resourceId) {
  state.selectedResource = resourceId;
  state.selectedCostPoint = null;
  state.currentCostRows = [];
  renderResourceList();
  $('costChartTitle').textContent = 'Daily cost trend';
  $('costChartSubtitle').textContent = resourceId;
  setOverallButtonVisible(true);
  $('costPointDetail').innerHTML = '<span class="muted">Click a resource-level cost point to inspect health signals for the same resource and day. Resource-level health overview is shown below until a cost point is selected.</span>';
  resetHealthDrilldown('Loading resource-level health overview from the same health API. Click a cost point to keep the existing same-day health drilldown.');
  try {
    const rows = await api('/api/cost', {run_id: state.runId, resource_id: resourceId});
    state.currentCostRows = rows;
    renderCostChart(rows, {mode: 'resource'});
  } catch (err) {
    console.warn('Resource cost trend unavailable', err);
    showEmpty($('costChart'), err.message || 'No cost rows for selected resource.');
    $('costLegend').innerHTML = '';
  }
  await loadResourceHealthOverview(resourceId);
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
  state.selectedCostPoint = null;
  const forecastRange = row.ForecastStart && row.ForecastEnd ? ` · Forecast ${esc(row.ForecastStart)} → ${esc(row.ForecastEnd)}` : '';
  const scope = mode === 'overall' ? 'Overall predicted cost' : 'Resource predicted cost';
  $('costPointDetail').innerHTML = `<strong>${esc(row.AnalysisDate)}</strong> · ${scope} <strong style="color:${PREDICTED_COST_COLOR}">${fmt(row.PredictedCost ?? row.CostAmount)}</strong>${forecastRange}<br/><span class="muted">Predicted point shown in a different color to bifurcate forecast from actual cost. Health drilldown is available only for actual historical cost points.</span>`;
  resetHealthDrilldown('Predicted cost points do not have historical health drilldown. Select an actual resource-level cost point to inspect health signals.');
}

function selectOverallCostPoint(row) {
  state.selectedCostPoint = null;
  $('costPointDetail').innerHTML = `<strong>${esc(row.AnalysisDate)}</strong> · Overall cost <strong>${fmt(row.CostAmount)}</strong><br/><span class="muted">Aggregate CostAmount across all analyzed resources in this run. Select a resource to inspect resource-level cost and health signals.</span>`;
  resetHealthDrilldown('Select a resource to inspect resource-level cost and health signals for a specific day.');
}

async function selectCostPoint(row) {
  const resourceId = state.selectedResource || row.ResourceID;
  if (!resourceId) return selectOverallCostPoint(row);
  state.selectedCostPoint = row;
  state.selectedHealthTimestamp = null;
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
  const series = (payload.series || []).filter(s => isGraphHealthSeries(s, payload));
  if (!series.length) return showEmpty(el, graphUnavailableMessage(payload));
  const azurePayload = isAzureHealthPayload(payload);
  const azurePercentGraph = hasAzurePercentGraph(payload);
  const azureContextGraph = azurePayload && !azurePercentGraph;
  const normalized = azureContextGraph || (!azurePercentGraph && state.healthMode === 'normalized');
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
  const relativeAxis = azurePercentGraph || normalized;
  const minY = relativeAxis ? 0 : Math.min(0, ...plotted), maxY = relativeAxis ? 100 : (Math.max(...plotted) || 1);
  const x = t => m.l + (times.length===1 ? .5 : byTime.get(t)/(times.length-1))*(w-m.l-m.r);
  const y = v => h-m.b - ((v-minY)/(maxY-minY || 1))*(h-m.t-m.b);
  const yTicks = relativeAxis ? [0, 25, 50, 75, 100] : [0,.25,.5,.75,1].map(p => minY + p*(maxY-minY));
  const yLabel = v => azurePercentGraph ? `${fmt(v, 0)}%` : fmt(v);
  const paths = series.map((s, si) => {
    const pts = (s.Points||[]).filter(p => !Number.isNaN(Number(p.Value))).map((p,i) => `${i?'L':'M'}${x(p.Timestamp)},${y(plotVal(s, Number(p.Value)))}`).join(' ');
    return `<path class="line" d="${pts}" stroke="${metricColor(si)}"></path>${(s.Points||[]).map((p, pi) => `<circle class="point" data-si="${si}" data-pi="${pi}" cx="${x(p.Timestamp)}" cy="${y(plotVal(s, Number(p.Value)))}" r="4" fill="${metricColor(si)}"></circle>`).join('')}`;
  }).join('');
  const contextNote = azureContextGraph ? '<div class="muted chart-note">Azure context graph: metrics with different units are normalized 0–100 per metric for visibility. Hover points and KPI cards retain the raw values/units; no percentage values are fabricated.</div>' : '';
  el.innerHTML = `${contextNote}<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    ${yTicks.map(v => `<line class="grid-line" x1="${m.l}" y1="${y(v)}" x2="${w-m.r}" y2="${y(v)}"></line><text class="axis-text" x="8" y="${y(v)+4}">${yLabel(v)}</text>`).join('')}
    <line class="axis" x1="${m.l}" y1="${h-m.b}" x2="${w-m.r}" y2="${h-m.b}"></line><line class="axis" x1="${m.l}" y1="${m.t}" x2="${m.l}" y2="${h-m.b}"></line>
    ${paths}
    ${times.map((t,i) => (i % Math.ceil(times.length/8)===0 || i===times.length-1) ? `<text class="axis-text" x="${x(t)-18}" y="${h-16}">${esc(healthAxisLabel(t, times))}</text>` : '').join('')}
  </svg>`;
  el.querySelectorAll('.point').forEach(pt => {
    const s = series[Number(pt.dataset.si)];
    const p = s && (s.Points || [])[Number(pt.dataset.pi)];
    if (!s || !p) return;
    pt.addEventListener('mouseenter', e => {
      const overviewBucket = ['PT24H', 'PT6H'].includes(p.Granularity);
      const unit = esc(s.Unit || '');
      const bucketAvg = p.BucketAverage ?? p.DailyAverage ?? p.Value;
      const bucketMax = p.BucketMax ?? p.DailyMax;
      const bucketLabel = p.Granularity || 'overview';
      const bucketParts = [`${bucketLabel} avg: ${fmt(bucketAvg)} ${unit}`];
      if (bucketMax !== undefined) bucketParts.push(`Bucket max: ${fmt(bucketMax)} ${unit}${p.PeakTimestamp ? ` at ${esc(p.PeakTimestamp)}` : ''}`);
      const extra = overviewBucket ? `<br/>${bucketParts.join('<br/>')}` : '';
      tooltip(e, `<strong>${esc(p.Timestamp)}</strong><br/>${esc(s.MetricCategory || s.MetricName)}: ${fmt(p.Value)} ${unit}${extra}${scriptScheduleTooltip(p)}`);
    });
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
    const points = m.Points || [];
    const vals = points.map(p => numOrNull(p.Value)).filter(v => v !== null);
    const peak = vals.length ? Math.max(...vals) : null;
    const avg = metricAverage(points);
    const peakPoint = points.find(p => numOrNull(p.Value) === peak) || {};
    const overviewBucket = points.some(p => ['PT24H', 'PT6H'].includes(p.Granularity));
    const granularityLabel = (points.find(p => ['PT24H', 'PT6H'].includes(p.Granularity)) || {}).Granularity;
    const pointLabel = overviewBucket ? `${points.length} ${granularityLabel || 'overview'} pts` : `${points.length} hourly pts`;
    const descParts = [`${esc(m.MetricName)} · ${esc(m.Unit || 'unit')} · ${pointLabel}`];
    if (peak !== null) descParts.push(`${overviewBucket ? 'bucket avg peak' : 'peak'} ${fmt(peak)}${m.Unit ? ` ${esc(m.Unit)}` : ''} at ${esc(healthAxisLabel(peakPoint.Timestamp, points.map(p => p.Timestamp)))}`);
    const bucketMax = numOrNull(peakPoint.BucketMax ?? peakPoint.DailyMax);
    if (overviewBucket && bucketMax !== null) descParts.push(`bucket max ${fmt(bucketMax)}${m.Unit ? ` ${esc(m.Unit)}` : ''}${peakPoint.PeakTimestamp ? ` at ${esc(peakPoint.PeakTimestamp.slice(5, 16).replace('T', ' '))}` : ''}`);
    if (avg !== null) descParts.push(`avg ${fmt(avg)}${m.Unit ? ` ${esc(m.Unit)}` : ''}`);
    const desc = descParts.join(' · ');
    return `
    <div class="health-card"><strong style="color:${metricColor(i)}">${esc(m.MetricCategory || m.MetricName)}</strong>
      <span class="muted" id="metricDesc-${i}" data-desc="${esc(desc)}">${esc(desc)}</span>
      <span class="muted mono" id="metricValue-${i}"></span>
    </div>`;
  }).join('') : '';
  const mongoPointKpis = isMongoHealthPayload(payload) ? '<div id="mongoPointKpis" class="mongo-point-kpis" style="grid-column:1/-1"><div class="health-card"><strong>MongoDB point KPIs</strong><span class="muted">Click an hourly MongoDB health point to show Tier, SlowQueryCount, and scheduled MachineData scripts for that exact hour.</span></div></div>' : '';
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
