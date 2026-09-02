const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

class ClassList {
  constructor(initial = '') { this.values = new Set(initial.split(/\s+/).filter(Boolean)); }
  add(v) { this.values.add(v); }
  remove(v) { this.values.delete(v); }
  toggle(v, force) { if (force === undefined ? !this.values.has(v) : force) this.add(v); else this.remove(v); }
  contains(v) { return this.values.has(v); }
  toString() { return [...this.values].join(' '); }
}

class Element {
  constructor(id = null) {
    this.id = id;
    this.value = '';
    this.dataset = {};
    this.listeners = {};
    this.childrenBySelector = new Map();
    this.classList = new ClassList();
    this._innerHTML = '';
    this._textContent = '';
  }
  set innerHTML(value) {
    this._innerHTML = String(value || '');
    this.childrenBySelector.clear();
    if (this.id === 'resourceList') {
      const rows = [...this._innerHTML.matchAll(/<div class="resource-row ([^"]*)" data-rid="([^"]*)"[^>]*>/g)].map(match => {
        const row = new Element();
        row.classList = new ClassList(`resource-row ${match[1]}`);
        row.dataset.rid = decodeEntities(match[2]);
        return row;
      });
      this.childrenBySelector.set('.resource-row', rows);
    }
    if (this.id === 'costChart') {
      const points = [...this._innerHTML.matchAll(/<circle class="point" data-idx="(\d+)"/g)].map(match => {
        const point = new Element();
        point.classList = new ClassList('point');
        point.dataset.idx = match[1];
        return point;
      });
      this.childrenBySelector.set('.point', points);
    }
  }
  get innerHTML() { return this._innerHTML; }
  set textContent(value) { this._textContent = String(value || ''); }
  get textContent() { return this._textContent; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  querySelectorAll(selector) { return this.childrenBySelector.get(selector) || []; }
  dispatch(type, event = {}) { (this.listeners[type] || []).forEach(fn => fn({preventDefault(){}, ...event})); }
}

function decodeEntities(value) {
  return value.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
}

function makeHarness() {
  const ids = ['refreshBtn','runSelect','resourceSearch','rawMode','normalizedMode','overallBtn','runMeta','summaryCards','resourceCount','resourceList','costChartTitle','costChartSubtitle','costLegend','costChart','costPointDetail','healthContext','healthChart','healthSummary'];
  const elements = Object.fromEntries(ids.map(id => [id, new Element(id)]));
  elements.costChart.clientWidth = 900;
  elements.healthChart.clientWidth = 900;
  elements.runSelect.selectedIndex = 0;
  elements.rawMode.classList.add('chip'); elements.rawMode.classList.add('active');
  elements.normalizedMode.classList.add('chip');
  elements.overallBtn.classList.add('chip'); elements.overallBtn.classList.add('hidden');
  const severityButtons = ['all', 'Critical', 'High', 'Medium', 'Low'].map(sev => {
    const btn = new Element();
    btn.dataset.severity = sev;
    btn.classList = new ClassList(sev === 'all' ? 'chip active' : 'chip');
    return btn;
  });
  const apiCalls = [];
  const context = {
    console,
    setInterval: () => 1,
    clearInterval: () => {},
    URL,
    fetch: () => { throw new Error('fetch should not be used in static test'); },
    document: {
      body: new Element('body'),
      createElement: () => new Element(),
      getElementById: id => elements[id],
      querySelectorAll: selector => {
        if (selector === '[data-severity]') return severityButtons;
        if (selector === '.resource-row') return elements.resourceList.querySelectorAll('.resource-row');
        return [];
      }
    },
    window: {
      location: {pathname: '/dashboard-plugins/cloudvitals/dist/cloudvitals.html'},
      innerWidth: 1200,
      CLOUDVITALS_STATIC_DATA: {
        latest_run_id: 'run1',
        runs: [
          {run_id: 'run1', fromDate: '2026-08-01', toDate: '2026-08-02', files: {}, has_health_timeseries: true},
          {run_id: 'run2', fromDate: '2026-09-01', toDate: '2026-09-02', files: {}, has_health_timeseries: false}
        ],
        summaries: {
          run1: {run_id: 'run1', fromDate: '2026-08-01', toDate: '2026-08-02', resource_count: 2, affected_resource_count: 2, cost_anomaly_records: 1, health_analysis_records: 1, health_records_with_unavailable_metrics: 0, health_timeseries_series_count: 1, has_health_timeseries: true},
          run2: {run_id: 'run2', fromDate: '2026-09-01', toDate: '2026-09-02', resource_count: 1, affected_resource_count: 1, cost_anomaly_records: 0, health_analysis_records: 0, health_records_with_unavailable_metrics: 0, has_health_timeseries: false}
        },
        resources: {
          run1: [
            {ResourceID: 'resource-a', ResourceName: 'Resource A', ResourceType: 'Compute', MaxSeverity: 'High', PeakCost: 200, AnomalyCount: 1, CostHealthCorrelation: 'No Clear Correlation'},
            {ResourceID: 'resource-b', ResourceName: 'Resource B', ResourceType: 'Storage', MaxSeverity: 'Low', PeakCost: 75, AnomalyCount: 0, CostHealthCorrelation: 'Insufficient Data'}
          ],
          run2: [{ResourceID: 'resource-c', ResourceName: 'Resource C', ResourceType: 'Database', MaxSeverity: 'Medium', PeakCost: 9, AnomalyCount: 0, CostHealthCorrelation: 'Not Available'}]
        },
        cost: {
          run1: {
            'resource-a': [
              {ResourceID: 'resource-a', AnalysisDate: '2026-08-01', CostAmount: 100, Severity: 'Normal', IsAnomaly: false, TrendStatus: 'Stable', AnalysisReason: 'normal'},
              {ResourceID: 'resource-a', AnalysisDate: '2026-08-02', CostAmount: 200, Severity: 'High', IsAnomaly: true, TrendStatus: 'Cost Spike', AnalysisReason: 'spike'}
            ],
            'resource-b': [
              {ResourceID: 'resource-b', AnalysisDate: '2026-08-01', CostAmount: 50, Severity: 'Low', IsAnomaly: false, TrendStatus: 'Stable', AnalysisReason: 'normal'},
              {ResourceID: 'resource-b', AnalysisDate: '2026-08-02', CostAmount: 75, Severity: 'Low', IsAnomaly: false, TrendStatus: 'Stable', AnalysisReason: 'normal'}
            ]
          },
          run2: {'resource-c': [{ResourceID: 'resource-c', AnalysisDate: '2026-09-01', CostAmount: 9, Severity: 'Medium', IsAnomaly: false, TrendStatus: 'Stable', AnalysisReason: 'normal'}]}
        },
        overallCost: {
          run1: [{AnalysisDate: '2026-08-01', CostAmount: 150}, {AnalysisDate: '2026-08-02', CostAmount: 275}],
          run2: [{AnalysisDate: '2026-09-01', CostAmount: 9}]
        },
        healthSummary: {run1: {'resource-a': {CostHealthCorrelation: 'No Clear Correlation', OverallHealthStatus: 'Healthy', HealthAnalysisReason: 'ok'}}},
        healthSeries: {run1: {'resource-a|2026-08-02': [{MetricCategory: 'CPU', MetricName: 'CPU', Unit: 'Percent', Points: [{Timestamp: '2026-08-02T00:00:00Z', Value: 80}]}]}}
      }
    }
  };
  const originalStaticApi = null;
  return {context, elements, apiCalls};
}

async function flush() { await Promise.resolve(); await Promise.resolve(); await new Promise(resolve => setImmediate(resolve)); }

(async () => {
  const {context, elements} = makeHarness();
  vm.createContext(context);
  const app = fs.readFileSync(path.join(__dirname, '..', 'static', 'app.js'), 'utf8');
  vm.runInContext(app, context, {filename: 'app.js'});
  await flush();

  assert.strictEqual(vm.runInContext('state.selectedResource', context), null, 'initial load should not select a resource');
  assert.strictEqual(elements.costChartTitle.textContent, 'Overall cost trend');
  assert.strictEqual(elements.costChartSubtitle.textContent, 'All analyzed resources');
  assert.strictEqual(elements.resourceList.querySelectorAll('.resource-row').filter(r => r.classList.contains('active')).length, 0);
  assert.strictEqual(elements.costChart.querySelectorAll('.point').length, 2, 'overall chart should render aggregate points');

  elements.costChart.querySelectorAll('.point')[1].dispatch('click');
  await flush();
  assert.strictEqual(vm.runInContext('state.lastHealthPayload', context), null, 'overall point click must not load health');
  assert.match(elements.costPointDetail.innerHTML, /Overall cost/);
  assert.match(elements.healthContext.textContent, /Select a resource/);

  await vm.runInContext("selectResource('resource-a')", context);
  await flush();
  assert.strictEqual(vm.runInContext('state.selectedResource', context), 'resource-a');
  assert.strictEqual(elements.costChartTitle.textContent, 'Daily cost trend');
  assert.strictEqual(elements.costChartSubtitle.textContent, 'resource-a');
  assert.strictEqual(elements.overallBtn.classList.contains('hidden'), false);
  assert.strictEqual(vm.runInContext('state.lastHealthPayload', context), null, 'resource selection must not auto-load health');
  assert.strictEqual(elements.resourceList.querySelectorAll('.resource-row').filter(r => r.classList.contains('active')).length, 1);

  elements.costChart.querySelectorAll('.point')[1].dispatch('click');
  await flush();
  const payload = vm.runInContext('state.lastHealthPayload', context);
  assert.strictEqual(payload.ResourceID, 'resource-a');
  assert.strictEqual(payload.date, '2026-08-02');
  assert.match(elements.healthContext.textContent, /resource-a · 2026-08-02/);

  elements.overallBtn.dispatch('click');
  await flush();
  assert.strictEqual(vm.runInContext('state.selectedResource', context), null);
  assert.strictEqual(elements.costChartTitle.textContent, 'Overall cost trend');
  assert.strictEqual(elements.overallBtn.classList.contains('hidden'), true);
  assert.strictEqual(elements.resourceList.querySelectorAll('.resource-row').filter(r => r.classList.contains('active')).length, 0);

  await vm.runInContext("loadRun('run2')", context);
  await flush();
  assert.strictEqual(vm.runInContext('state.selectedResource', context), null, 'run change resets to overall mode');
  assert.strictEqual(elements.costChartTitle.textContent, 'Overall cost trend');
  assert.strictEqual(elements.costChartSubtitle.textContent, 'All analyzed resources');

  await vm.runInContext("loadRun('run1')", context);
  await flush();
  elements.resourceSearch.value = 'Storage';
  vm.runInContext('renderResourceList()', context);
  assert.strictEqual(elements.resourceList.querySelectorAll('.resource-row').length, 1, 'search filter should still narrow resources');
  assert.strictEqual(elements.resourceList.querySelectorAll('.resource-row')[0].dataset.rid, 'resource-b');

  console.log('frontend smoke tests passed');
})().catch(err => {
  console.error(err);
  process.exit(1);
});
