#!/usr/bin/env python3
"""Publish the latest Cost/Health run as the Hermes /dashboard CloudVitals plugin."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path('/opt/data/cost-health-dashboard')
PLUGIN = Path('/opt/data/plugins/cloudvitals/dashboard')
DATA_DIR = Path('/opt/data')
sys.path.insert(0, str(ROOT))
import dashboard_api  # noqa: E402


def static_plugin_html() -> str:
    html = (ROOT / 'static' / 'index.html').read_text(encoding='utf-8')
    html = html.replace('href="/styles.css"', 'href="styles.css"')
    html = html.replace('src="/app.js"', 'src="app.js"')
    if 'src="data.js"' not in html:
        html = html.replace('<script src="app.js"></script>', '<script src="data.js"></script>\n  <script src="app.js"></script>')
    return html


def _group_split_health_series(rows: list[dict], source: str) -> dict[str, list[dict]]:
    """Build static healthSeries map keyed by ResourceID|Date from split health artifacts."""
    grouped: dict[str, list[dict]] = {}
    preferred = ["CPU", "MemoryUsage", "Disk", "Network", "SNAT", "TrafficGiB", "AvgConn", "SNATPeak", "StorageSize", "IndexSize", "LongRunningSlowQueries", "Connections", "IOPs"]
    rank = {name: i for i, name in enumerate(preferred)}
    for row in rows:
        rid = row.get('ResourceID')
        metrics = row.get('Metrics') or {}
        if not rid or not isinstance(metrics, dict):
            continue
        for category, points in metrics.items():
            by_day: dict[str, list[dict]] = {}
            if not isinstance(points, list):
                continue
            for point in points:
                if not isinstance(point, dict) or point.get('Value') is None:
                    continue
                ts = str(point.get('Timestamp') or '')
                if len(ts) < 10:
                    continue
                by_day.setdefault(ts[:10], []).append(point)
            for date, pts in by_day.items():
                pts = sorted(pts, key=lambda p: p.get('Timestamp') or '')
                first = pts[0]
                grouped.setdefault(f"{rid}|{date}", []).append({
                    'ResourceID': rid,
                    'ResourceType': row.get('ResourceType'),
                    'Date': date,
                    'MetricCategory': str(category),
                    'MetricName': first.get('MetricName') or str(category),
                    'Unit': first.get('Unit') or 'Unknown',
                    'Aggregation': first.get('Aggregation') or row.get('Aggregation') or 'Hourly',
                    'HealthSource': row.get('HealthSource') or source,
                    'Points': [{'Timestamp': p.get('Timestamp'), 'Value': p.get('Value')} for p in pts],
                })
    for key, items in grouped.items():
        items.sort(key=lambda s: (rank.get(s.get('MetricCategory'), 999), s.get('MetricCategory') or ''))
    return grouped


def publish(run_id: str = 'latest') -> dict:
    PLUGIN.mkdir(parents=True, exist_ok=True)
    dist = PLUGIN / 'dist'
    dist.mkdir(parents=True, exist_ok=True)
    for name in ['styles.css', 'app.js']:
        shutil.copyfile(ROOT / 'static' / name, dist / name)
    (dist / 'cloudvitals.html').write_text(static_plugin_html(), encoding='utf-8')
    run = dashboard_api.get_run(DATA_DIR, run_id)
    resolved = run['run_id']
    resources = dashboard_api.affected_resources(DATA_DIR, resolved)
    cost = {r['ResourceID']: dashboard_api.cost_timeseries(DATA_DIR, resolved, r['ResourceID']) for r in resources}
    overall_cost = dashboard_api.overall_cost_timeseries(DATA_DIR, resolved)
    health_summary = dashboard_api.health_summary_map(DATA_DIR, resolved)
    health_series = {}
    azure_health_rows = []
    mongo_health_rows = []
    az_path = run['files'].get('azure_health')
    if az_path and Path(az_path).exists():
        with open(az_path, encoding='utf-8') as fh:
            azure_health_rows = json.load(fh)
        health_series.update(_group_split_health_series(azure_health_rows, 'AzureMonitor'))
    mongo_path = run['files'].get('mongo_health')
    if mongo_path and Path(mongo_path).exists():
        with open(mongo_path, encoding='utf-8') as fh:
            mongo_health_rows = json.load(fh)
        for key, items in _group_split_health_series(mongo_health_rows, 'MongoDBCommandsOnly').items():
            health_series.setdefault(key, []).extend(items)
    ts_path = run['files'].get('health_timeseries')
    if ts_path and Path(ts_path).exists():
        with open(ts_path, encoding='utf-8') as fh:
            health_items = json.load(fh)
        for item in health_items:
            key = f"{item.get('ResourceID')}|{item.get('Date', '')}"
            health_series.setdefault(key, []).append(item)
    payload = {
        'generated_from': str(DATA_DIR),
        'latest_run_id': resolved,
        'runs': [{k: run[k] for k in ['run_id', 'fromDate', 'toDate', 'files', 'has_health_timeseries']}],
        'summaries': {resolved: dashboard_api.run_summary(DATA_DIR, resolved)},
        'resources': {resolved: resources},
        'cost': {resolved: cost},
        'overallCost': {resolved: overall_cost},
        'healthSummary': {resolved: health_summary},
        'azureHealth': {resolved: azure_health_rows},
        'mongoHealth': {resolved: mongo_health_rows},
        'healthSeries': {resolved: health_series},
    }
    data_path = dist / 'data.js'
    data_js = 'window.CLOUDVITALS_STATIC_DATA = ' + json.dumps(payload, ensure_ascii=False, separators=(',', ':')) + ';\n'
    data_path.write_text(data_js, encoding='utf-8')
    # Keep legacy dashboard-root data.js for any already-cached iframe/html path.
    (PLUGIN / 'data.js').write_text(data_js, encoding='utf-8')
    return {
        'ok': True,
        'run_id': resolved,
        'plugin_dir': str(PLUGIN),
        'data_file': str(data_path),
        'data_file_bytes': data_path.stat().st_size,
        'resource_count': len(resources),
        'cost_points': sum(len(v) for v in cost.values()),
        'overall_cost_points': len(overall_cost),
        'health_series': sum(len(v) for v in health_series.values()),
    }


if __name__ == '__main__':
    run_id = sys.argv[1] if len(sys.argv) > 1 else 'latest'
    print(json.dumps(publish(run_id), indent=2))
