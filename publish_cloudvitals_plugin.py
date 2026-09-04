#!/usr/bin/env python3
"""Publish the latest Cost/Health run as the Hermes /dashboard CloudVitals plugin."""
from __future__ import annotations

import json
import hashlib
import shutil
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path('/opt/data/cost-health-dashboard')
PLUGIN = Path('/opt/data/plugins/cloudvitals/dashboard')
DATA_DIR = Path('/opt/data')
sys.path.insert(0, str(ROOT))
import dashboard_api  # noqa: E402


def static_plugin_html(cache_bust: str | None = None) -> str:
    html = (ROOT / 'static' / 'index.html').read_text(encoding='utf-8')
    suffix = f'?v={cache_bust}' if cache_bust else ''
    html = html.replace('href="/styles.css"', f'href="styles.css{suffix}"')
    html = html.replace('src="/app.js"', f'src="app.js{suffix}"')
    if 'src="data.js"' not in html:
        html = html.replace(f'<script src="app.js{suffix}"></script>', f'<script src="data.js{suffix}"></script>\n  <script src="app.js{suffix}"></script>')
    return html


def compact_health_point(point: dict) -> dict:
    """Keep plotted value plus per-point KPI metadata in health shards."""
    compact = {'Timestamp': point.get('Timestamp'), 'Value': point.get('Value')}
    for key in ['Tier', 'SlowQueryCount', 'SlowQueryNamespaces', 'MemoryTotalMB', 'CpuCores', 'MemoryResidentMB', 'StorageSizeMB', 'ScriptScheduleContext', 'ScheduledScripts', 'DailyAverage', 'DailyMax', 'DailyMin', 'DailyPointCount', 'BucketAverage', 'BucketMax', 'BucketMin', 'BucketPointCount', 'PeakTimestamp', 'Granularity', 'Aggregation']:
        if key in point:
            compact[key] = point.get(key)
    return compact


def _safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bucket_start_iso(timestamp: str, hours: int = 6) -> str | None:
    try:
        dt = datetime.fromisoformat(str(timestamp).replace('Z', '+00:00'))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    dt = dt.replace(hour=(dt.hour // hours) * hours)
    return dt.strftime('%Y-%m-%dT%H:00:00Z')


def _six_hour_overview_points(points: list[dict]) -> list[dict]:
    by_bucket: dict[str, list[dict]] = defaultdict(list)
    for point in points:
        bucket = _bucket_start_iso(str(point.get('Timestamp') or ''), 6)
        if bucket:
            by_bucket[bucket].append(point)
    output = []
    for bucket, bucket_points in sorted(by_bucket.items()):
        numeric = [(p, _safe_float(p.get('Value'))) for p in bucket_points]
        numeric = [(p, v) for p, v in numeric if v is not None]
        if not numeric:
            continue
        values = [v for _p, v in numeric]
        max_point, max_value = max(numeric, key=lambda item: item[1])
        min_point, min_value = min(numeric, key=lambda item: item[1])
        avg_value = sum(values) / len(values)
        compact = {
            'Timestamp': bucket,
            'Value': avg_value,
            'BucketAverage': avg_value,
            'BucketMax': max_value,
            'BucketMin': min_value,
            'BucketPointCount': len(values),
            'PeakTimestamp': max_point.get('Timestamp'),
            'Aggregation': 'Average',
            'Granularity': 'PT6H',
        }
        for key in ['Tier', 'SlowQueryCount', 'SlowQueryNamespaces', 'MemoryTotalMB', 'CpuCores', 'MemoryResidentMB', 'StorageSizeMB', 'ScriptScheduleContext', 'ScheduledScripts']:
            if key in max_point:
                compact[key] = max_point.get(key)
        output.append(compact)
    return output


def _group_split_health_series(rows: list[dict], source: str) -> dict[str, list[dict]]:
    """Build static healthSeries map keyed by ResourceID|Date from split health artifacts."""
    grouped: dict[str, list[dict]] = {}
    mongo_graph_categories = {"Connections", "MemoryUsage", "StorageSize"}
    preferred = ["CPU", "MemoryUsage", "Disk", "Network", "SNAT", "TrafficGiB", "AvgConn", "SNATPeak", "Connections", "StorageSize", "IOPs"]
    mongo_preferred = ["Connections", "MemoryUsage", "StorageSize"]
    rank = {name: i for i, name in enumerate(preferred)}
    mongo_rank = {name: i for i, name in enumerate(mongo_preferred)}
    for row in rows:
        rid = row.get('ResourceID')
        metrics = row.get('Metrics') or {}
        overview_metrics = row.get('OverviewMetrics') or {}
        if not rid or not isinstance(metrics, dict):
            continue
        row_is_mongo = source == 'MongoDBCommandsOnly' or any(marker in str(row.get('HealthSource') or '') for marker in ['MongoDB', 'MongoAtlas'])
        for category, points in metrics.items():
            if row_is_mongo and str(category) not in mongo_graph_categories:
                continue
            by_day: dict[str, list[dict]] = {}
            if not isinstance(points, list):
                continue
            usable_points = []
            for point in points:
                if not isinstance(point, dict) or point.get('Value') is None:
                    continue
                ts = str(point.get('Timestamp') or '')
                if len(ts) < 10:
                    continue
                usable_points.append(point)
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
                    'Points': [compact_health_point(p) for p in pts],
                })
            if row_is_mongo and usable_points:
                pts = sorted(usable_points, key=lambda p: p.get('Timestamp') or '')
                first = pts[0]
                grouped.setdefault(f"{rid}|", []).append({
                    'ResourceID': rid,
                    'ResourceType': row.get('ResourceType'),
                    'Date': None,
                    'MetricCategory': str(category),
                    'MetricName': first.get('MetricName') or str(category),
                    'Unit': first.get('Unit') or 'Unknown',
                    'Aggregation': 'Average',
                    'Granularity': 'PT6H',
                    'HealthSource': row.get('HealthSource') or source,
                    'Points': [compact_health_point(p) for p in _six_hour_overview_points(pts)],
                })
        if source == 'AzureMonitor' and isinstance(overview_metrics, dict):
            # Resource overview must come from a second Azure Monitor call using
            # interval=PT6H and aggregation=Average, not from client-side hourly rollups.
            for category, points in overview_metrics.items():
                if not isinstance(points, list):
                    continue
                pts = sorted([p for p in points if isinstance(p, dict) and p.get('Value') is not None and str(p.get('Timestamp') or '')], key=lambda p: p.get('Timestamp') or '')
                if not pts:
                    continue
                first = pts[0]
                grouped.setdefault(f"{rid}|", []).append({
                    'ResourceID': rid,
                    'ResourceType': row.get('ResourceType'),
                    'Date': None,
                    'MetricCategory': str(category),
                    'MetricName': first.get('MetricName') or str(category),
                    'Unit': first.get('Unit') or 'Unknown',
                    'Aggregation': 'Average',
                    'Granularity': 'PT6H',
                    'HealthSource': row.get('HealthSource') or source,
                    'Points': [compact_health_point(p) for p in pts],
                })
    for key, items in grouped.items():
        if _health_kind_for_series(items) == 'mongodb':
            items.sort(key=lambda s: (mongo_rank.get(s.get('MetricCategory'), 999), s.get('MetricCategory') or ''))
        else:
            items.sort(key=lambda s: (rank.get(s.get('MetricCategory'), 999), s.get('MetricCategory') or ''))
    return grouped


def _health_kind_for_series(items: list[dict]) -> str:
    has_mongo = any(
        any(marker in str(item.get('HealthSource') or '') for marker in ['MongoDB', 'MongoAtlas'])
        or item.get('MetricCategory') in {'StorageSize', 'Connections', 'AtlasTier', 'SlowQueryCount', 'SlowQueryNamespaces'}
        for item in items
    )
    has_azure = any(
        'Azure' in str(item.get('HealthSource') or '')
        or (
            not any(marker in str(item.get('HealthSource') or '') for marker in ['MongoDB', 'MongoAtlas'])
            and item.get('MetricCategory') in {'CPU', 'MemoryUsage', 'Disk', 'IOPs', 'Network', 'SNAT', 'TrafficGiB', 'AvgConn', 'SNATPeak'}
        )
        for item in items
    )
    if has_mongo and not has_azure:
        return 'mongodb'
    if has_azure and not has_mongo:
        return 'azure'
    return 'mixed' if items else None


def _source_for_series(items: list[dict]) -> str:
    kind = _health_kind_for_series(items)
    if kind == 'mongodb':
        return 'Mongo_Health_Analysis static shard'
    if kind == 'azure':
        return 'Azure_Health_Analysis static shard'
    return 'Split health analysis static shard'


def _minimal_health_coverage(rows: list[dict], source: str) -> dict[str, list[dict]]:
    """Return compact per-resource coverage metadata for no-data messages.

    The full split health rows can be hundreds of MB because they contain every
    hourly point. Keep those points out of data.js; retain just enough metadata
    for the UI to explain why a clicked resource/date has no graph.
    """
    coverage: dict[str, list[dict]] = {}
    for row in rows:
        rid = row.get('ResourceID')
        if not rid:
            continue
        metric_errors = []
        for err in row.get('MetricErrors') or []:
            if isinstance(err, dict):
                metric_errors.append({k: err.get(k) for k in ['Stage', 'MetricCategory', 'MetricName', 'Error'] if err.get(k) is not None})
            else:
                metric_errors.append({'Error': str(err)})
            if len(metric_errors) >= 3:
                break
        coverage.setdefault(rid, []).append({
            'source': source,
            'ResourceType': row.get('ResourceType'),
            'HealthSource': row.get('HealthSource'),
            'TemporalCoverage': row.get('TemporalCoverage'),
            'CoverageNote': row.get('CoverageNote'),
            'MetricErrors': metric_errors,
            'SnapshotCount': row.get('SnapshotCount'),
        })
    return coverage


def publish(run_id: str = 'latest') -> dict:
    PLUGIN.mkdir(parents=True, exist_ok=True)
    dist = PLUGIN / 'dist'
    dist.mkdir(parents=True, exist_ok=True)
    for name in ['styles.css', 'app.js']:
        shutil.copyfile(ROOT / 'static' / name, dist / name)
    run = dashboard_api.get_run(DATA_DIR, run_id)
    resolved = run['run_id']
    (dist / 'cloudvitals.html').write_text(static_plugin_html(resolved), encoding='utf-8')
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
        for row in mongo_health_rows:
            if not isinstance(row, dict):
                continue
            for points in (row.get('Metrics') or {}).values():
                if not isinstance(points, list):
                    continue
                for point in points:
                    if not isinstance(point, dict) or not point.get('Timestamp'):
                        continue
                    script_context = dashboard_api.script_schedule_context_for_mongo_point(row, str(point.get('Timestamp')))
                    if script_context:
                        point['ScriptScheduleContext'] = script_context
                        point['ScheduledScripts'] = script_context.get('scheduled_scripts', [])
        for key, items in _group_split_health_series(mongo_health_rows, 'MongoDBCommandsOnly').items():
            health_series.setdefault(key, []).extend(items)
    ts_path = run['files'].get('health_timeseries')
    if ts_path and Path(ts_path).exists():
        with open(ts_path, encoding='utf-8') as fh:
            health_items = json.load(fh)
        for item in health_items:
            key = f"{item.get('ResourceID')}|{item.get('Date', '')}"
            health_series.setdefault(key, []).append(item)
    health_dir = dist / 'health' / resolved
    if health_dir.exists():
        shutil.rmtree(health_dir)
    health_dir.mkdir(parents=True, exist_ok=True)
    health_index: dict[str, dict] = {}
    for key, items in health_series.items():
        shard_name = hashlib.sha256(key.encode('utf-8')).hexdigest()[:24] + '.json'
        shard_rel = f'health/{resolved}/{shard_name}'
        shard_path = dist / shard_rel
        payload = {
            'source': _source_for_series(items),
            'health_kind': _health_kind_for_series(items),
            'series': items,
        }
        shard_path.write_text(json.dumps(payload, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
        health_index[key] = {
            'file': shard_rel,
            'source': payload['source'],
            'health_kind': payload['health_kind'],
            'series_count': len(items),
        }
    health_coverage: dict[str, list[dict]] = {}
    for rid, items in _minimal_health_coverage(azure_health_rows, 'Azure_Health_Analysis').items():
        health_coverage.setdefault(rid, []).extend(items)
    for rid, items in _minimal_health_coverage(mongo_health_rows, 'Mongo_Health_Analysis').items():
        health_coverage.setdefault(rid, []).extend(items)
    payload = {
        'generated_from': str(DATA_DIR),
        'latest_run_id': resolved,
        'runs': [{k: run[k] for k in ['run_id', 'fromDate', 'toDate', 'files', 'has_health_timeseries']}],
        'summaries': {resolved: dashboard_api.run_summary(DATA_DIR, resolved)},
        'resources': {resolved: resources},
        'cost': {resolved: cost},
        'overallCost': {resolved: overall_cost},
        'healthSummary': {resolved: health_summary},
        # Keep the initial bundle small. Hourly health graph data is loaded on
        # demand from per-resource/day static shards instead of eagerly parsing a
        # 100MB+ JavaScript object during dashboard startup.
        'healthIndex': {resolved: health_index},
        'healthCoverage': {resolved: health_coverage},
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
        'health_shards': len(health_index),
    }


if __name__ == '__main__':
    run_id = sys.argv[1] if len(sys.argv) > 1 else 'latest'
    print(json.dumps(publish(run_id), indent=2))
