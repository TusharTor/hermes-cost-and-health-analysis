#!/usr/bin/env python3
"""Generate separate Azure and MongoDB hourly health analysis files.

Outputs:
- Azure_Health_Analysis_DDMMYY_HHMMSS.json
- Mongo_Health_Analysis_DDMMYYYY_HHMMSS.json

The Cost-Analysis artifact is left untouched. The generated files are keyed by
ResourceID so the dashboard can bind a clicked daily cost point to same-resource,
same-day hourly health graphs.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import stat
import subprocess
import sys
import tempfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import dashboard_api

CRED_PATH = Path('/opt/data/azure_credentials/azure_management.json.enc')
SNAPSHOT_FILE = Path('/opt/data/MongoDB-Command-Snapshots.jsonl')
MGMT = 'https://management.azure.com'

AZURE_CATEGORY_PATTERNS: dict[str, list[str]] = {
    'CPU': ['percentagecpu', 'cpu', 'cputime'],
    'MemoryUsage': ['memory', 'workingset'],
    'Disk': ['filesystem', 'usedcapacity', 'capacity', 'storage', 'disk'],
    'IOPs': ['iops', 'readops', 'writeops', 'transactions', 'operations', 'io'],
    'Network': ['network', 'ingress', 'egress', 'bytesreceived', 'bytessent', 'bandwidth', 'bytes'],
    'SNAT': ['snat', 'port'],
}
AZURE_CATEGORY_ORDER = list(AZURE_CATEGORY_PATTERNS)
NAT_GATEWAY_CATEGORY_ORDER = ['TrafficGiB', 'AvgConn', 'SNATPeak']
NAT_GATEWAY_METRICS: dict[str, dict[str, Any]] = {
    'TrafficGiB': {'MetricName': 'ByteCount', 'Aggregation': 'Total', 'Unit': 'GiB', 'Scale': 1 / (1024 ** 3)},
    'AvgConn': {'MetricName': 'TotalConnectionCount', 'Aggregation': 'Average', 'Unit': 'Count', 'Scale': 1.0},
    'SNATPeak': {'MetricName': 'SNATConnectionCount', 'Aggregation': 'Maximum', 'Unit': 'Count', 'Scale': 1.0},
}
MONGO_METRIC_ORDER = ['StorageSize', 'IndexSize', 'LongRunningSlowQueries', 'Connections', 'IOPs']
GENERIC_MONGO_KEYS = {
    'mongo_connection_string', 'mongodb_connection_string', 'mongoconnectionstring',
    'mongodb_uri', 'mongo_uri', 'connection_string', 'mongo_db_connection_string'
}


def mongo_run_id_from_run_id(run_id: str) -> str:
    """Convert DDMMYY_HHMMSS to DDMMYYYY_HHMMSS for Mongo file names."""
    try:
        date_part, time_part = run_id.split('_', 1)
        if len(date_part) == 6:
            return f'{date_part[:4]}20{date_part[4:]}_{time_part}'
    except ValueError:
        pass
    return run_id


def decrypt_credentials(password: str) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as td:
        pw_path = Path(td) / 'pw'
        pw_path.write_text(password, encoding='utf-8')
        os.chmod(pw_path, stat.S_IRUSR | stat.S_IWUSR)
        cp = subprocess.run([
            'openssl', 'enc', '-d', '-aes-256-cbc', '-pbkdf2', '-iter', '200000', '-md', 'sha256',
            '-in', str(CRED_PATH), '-pass', f'file:{pw_path}'
        ], capture_output=True)
    if cp.returncode != 0:
        raise RuntimeError('Credential decrypt failed.')
    data = json.loads(cp.stdout.decode('utf-8'))
    if not isinstance(data, dict):
        raise RuntimeError('Credential payload is not a JSON object.')
    return data


def cred(data: dict[str, Any], *names: str) -> Any:
    lowered = {str(k).lower(): v for k, v in data.items()}
    for name in names:
        if name in data:
            return data[name]
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


def is_arm_id(resource_id: str) -> bool:
    return isinstance(resource_id, str) and resource_id.lower().startswith('/subscriptions/') and '/providers/' in resource_id.lower()


def is_nat_gateway_id(resource_id: str) -> bool:
    return isinstance(resource_id, str) and 'microsoft.network/natgateways' in resource_id.lower()


def resource_subscription_id(resource_id: str) -> str | None:
    if not isinstance(resource_id, str):
        return None
    parts = [p for p in resource_id.split('/') if p]
    lowered = [p.lower() for p in parts]
    if 'subscriptions' not in lowered:
        return None
    idx = lowered.index('subscriptions')
    if idx + 1 >= len(parts):
        return None
    return parts[idx + 1]


def configured_subscription_id(creds: dict[str, Any]) -> str | None:
    value = cred(creds, 'subscription_id', 'subscriptionId', 'SubscriptionId', 'AZURE_SUBSCRIPTION_ID')
    return str(value).strip() if value else None


def azure_credential_profiles(creds: dict[str, Any]) -> list[dict[str, Any]]:
    """Return ARM credential profiles keyed by subscription.

    Supports the newer nested credential sections (AzureAd/DevTorAd/KoelAd)
    while keeping the legacy top-level Azure OAuth fields as a fallback.
    """
    profile_names = ['AzureAd', 'DevTorAd', 'KoelAd']
    profiles: list[dict[str, Any]] = []

    def build_profile(name: str, src: dict[str, Any]) -> dict[str, Any] | None:
        tenant = cred(src, 'tenant_id', 'tenantId', 'TenantId', 'AZURE_TENANT_ID')
        client_id = cred(src, 'client_id', 'clientId', 'ClientId', 'AZURE_CLIENT_ID')
        client_secret = cred(src, 'client_secret', 'clientSecret', 'ClientSecret', 'AZURE_CLIENT_SECRET')
        subscription_id = cred(src, 'subscription_id', 'subscriptionId', 'SubscriptionId', 'AZURE_SUBSCRIPTION_ID')
        if not all([tenant, client_id, client_secret, subscription_id]):
            return None
        instance = str(cred(src, 'Instance', 'instance') or 'https://login.microsoftonline.com/').rstrip('/') + '/'
        token_url = cred(src, 'token_url', 'tokenUrl', 'TokenUrl') or f'{instance}{tenant}/oauth2/v2.0/token'
        return {
            'ProfileName': name,
            'tenant_id': str(tenant).strip(),
            'client_id': str(client_id).strip(),
            'client_secret': str(client_secret),
            'subscription_id': str(subscription_id).strip(),
            'SubscriptionId': str(subscription_id).strip(),
            'token_url': str(token_url).strip(),
            'scope': str(cred(src, 'scope', 'Scope') or 'https://management.azure.com/.default'),
        }

    for name in profile_names:
        section = creds.get(name)
        if isinstance(section, dict):
            profile = build_profile(name, section)
            if profile:
                profiles.append(profile)
    legacy = build_profile('TopLevelAzure', creds)
    if legacy:
        profiles.append(legacy)
    # Prefer explicit named sections when duplicate subscriptions exist.
    deduped: dict[str, dict[str, Any]] = {}
    for profile in reversed(profiles):
        deduped[profile['subscription_id'].lower()] = profile
    return sorted(deduped.values(), key=lambda p: p['ProfileName'])


def is_mongo_uri(value: Any) -> bool:
    return isinstance(value, str) and value.strip().startswith(('mongodb://', 'mongodb+srv://'))


def credential_value(creds: dict[str, Any], key: str) -> Any:
    lowered = {str(k).lower(): k for k in creds.keys()}
    real = lowered.get(str(key).lower())
    return creds.get(real) if real else None


def resolve_mongodb_credential_key(creds: dict[str, Any], rid: str) -> tuple[str | None, str | None]:
    """Resolve resource-id-to-MongoDB credential mapping without querying health collections."""
    low = str(rid or '').lower()
    norm = ''.join(ch for ch in low if ch.isalnum())
    if low.startswith('/subscriptions/'):
        parts = [p.lower() for p in str(rid or '').split('/') if p]
        provider_type = None
        if 'providers' in parts:
            i = parts.index('providers')
            if i + 2 < len(parts):
                provider_type = (parts[i + 1], parts[i + 2])
        looks_mongo = provider_type in {('microsoft.documentdb', 'mongoclusters'), ('microsoft.documentdb', 'databaseaccounts')}
    else:
        looks_mongo = norm in {'platformmongodb', 'piaggiomongodb', 'johndeeremongodb', 'koelmongodb'} or norm.endswith('mongodb')
    if not looks_mongo:
        return None, None
    candidates: list[str] = []
    if 'piaggio' in low or 'tazuremongo-piaggio' in low or norm == 'piaggiomongodb':
        candidates.extend(['Piaggio_MongoDB', 'Piaggio_MongoDb', 'Piaggio_MongoDBConnectionString'])
    if 'john-deere' in low or 'johndeere' in low or 'john_deere' in low or norm == 'johndeeremongodb':
        candidates.extend(['John-Deere_MongoDB', 'John-Deere_MongoDb', 'JohnDeere_MongoDB'])
    if 'platform' in low or 'tazuremongodb' in low or norm == 'platformmongodb':
        candidates.extend(['Platform_MongoDB', 'Platform_MongoDb', 'Platform_MongoDBConnectionString'])
    if 'koel' in low or 'koelmongo' in low or norm == 'koelmongodb':
        candidates.extend(['Koel_MongoDB', 'Koel_MongoDb', 'Koel_MongoDBConnectionString'])
    candidates.append(str(rid or ''))
    seen: set[str] = set()
    for key in candidates:
        if not key or key in seen:
            continue
        seen.add(key)
        value = credential_value(creds, key)
        if is_mongo_uri(value):
            return key, value.strip()
    return None, None


def http_json(method: str, url: str, headers: dict[str, str] | None = None, data: dict[str, str] | None = None, timeout: int = 45) -> dict[str, Any]:
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen
    encoded = urlencode(data).encode('utf-8') if data else None
    req = Request(url, data=encoded, method=method, headers=headers or {})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')[:700]
        raise RuntimeError(f'HTTP {e.code}: {body}') from e
    except URLError as e:
        raise RuntimeError(f'URL error: {e.reason}') from e


def get_token(creds: dict[str, Any]) -> str:
    tenant = cred(creds, 'tenant_id', 'tenantId', 'TenantId')
    client_id = cred(creds, 'client_id', 'clientId', 'ClientId')
    client_secret = cred(creds, 'client_secret', 'clientSecret', 'ClientSecret')
    if not all([tenant, client_id, client_secret]):
        raise RuntimeError('Azure OAuth tenant/client/secret fields are missing.')
    token_url = cred(creds, 'token_url', 'tokenUrl') or f'https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token'
    scope = cred(creds, 'scope') or 'https://management.azure.com/.default'
    payload = http_json('POST', token_url, data={
        'grant_type': 'client_credentials', 'client_id': client_id, 'client_secret': client_secret, 'scope': scope
    }, timeout=30)
    token = payload.get('access_token')
    if not token:
        raise RuntimeError('Token response did not contain access_token.')
    return token


def normalized_name(name: str) -> str:
    return ''.join(ch for ch in str(name).lower() if ch.isalnum())


def azure_category(metric_name: str) -> str | None:
    n = normalized_name(metric_name)
    for category, patterns in AZURE_CATEGORY_PATTERNS.items():
        if any(pattern in n for pattern in patterns):
            return category
    return None


def metric_definitions(resource_id: str, token: str) -> list[dict[str, Any]]:
    url = f'{MGMT}{resource_id}/providers/microsoft.insights/metricDefinitions?api-version=2018-01-01'
    return http_json('GET', url, headers={'Authorization': f'Bearer {token}'}, timeout=45).get('value', [])


def azure_metric_preference(category: str, metric_name: str, unit: str | None) -> tuple[int, str]:
    """Prefer percentage CPU/memory metrics so dashboard health charts can use a true % Y-axis."""
    n = normalized_name(metric_name)
    u = str(unit or '').lower()
    score = 100
    if category == 'CPU':
        if 'percent' in n or 'percentage' in n or u == 'percent':
            score = 0
        elif 'time' in n or u in {'seconds', 'milliseconds'}:
            score = 50
    elif category == 'MemoryUsage':
        if 'percent' in n or 'percentage' in n or u == 'percent':
            score = 0
        elif 'availablememory' in n or 'workingset' in n or 'usedmemory' in n or u in {'bytes', 'byte', 'megabytes', 'kilobytes'}:
            score = 50
    return score, n


def choose_azure_metrics(defs: list[dict[str, Any]], per_category: int) -> dict[str, list[dict[str, str]]]:
    candidates: dict[str, list[dict[str, str]]] = {category: [] for category in AZURE_CATEGORY_ORDER}
    seen: set[str] = set()
    for item in defs:
        obj = item.get('name') or {}
        name = obj.get('value') or obj.get('localizedValue')
        if not isinstance(name, str) or name in seen:
            continue
        category = azure_category(name)
        if not category:
            continue
        seen.add(name)
        candidates[category].append({'MetricName': name, 'Unit': item.get('unit') or 'Unknown', 'PrimaryAggregation': item.get('primaryAggregationType') or 'Average'})
    chosen: dict[str, list[dict[str, str]]] = {category: [] for category in AZURE_CATEGORY_ORDER}
    for category, items in candidates.items():
        items.sort(key=lambda item: azure_metric_preference(category, item.get('MetricName') or '', item.get('Unit')))
        chosen[category] = items[:per_category]
    return chosen


def query_metric(
    resource_id: str,
    metric_name: str,
    token: str,
    from_date: str,
    to_date: str,
    preferred_aggregation: str | None = None,
    output_unit: str | None = None,
    value_scale: float = 1.0,
    interval: str = 'PT1H',
    aggregation_param: str = 'Average,Maximum,Total',
) -> tuple[list[dict[str, Any]], str | None]:
    end = (datetime.fromisoformat(to_date).replace(tzinfo=timezone.utc) + timedelta(days=1)).strftime('%Y-%m-%dT00:00:00Z')
    params = urlencode({
        'api-version': '2018-01-01',
        'timespan': f'{from_date}T00:00:00Z/{end}',
        'interval': interval,
        'metricnames': metric_name,
        'aggregation': aggregation_param,
    }, quote_via=quote)
    url = f'{MGMT}{resource_id}/providers/microsoft.insights/metrics?{params}'
    try:
        data = http_json('GET', url, headers={'Authorization': f'Bearer {token}'}, timeout=60)
    except Exception as e:
        return [], str(e)
    out: list[dict[str, Any]] = []
    for item in data.get('value', []) or []:
        unit = item.get('unit') or 'Unknown'
        for ts in item.get('timeseries', []) or []:
            for dp in ts.get('data', []) or []:
                numeric = {k: dp.get(k) for k in ('average', 'maximum', 'total') if dp.get(k) is not None}
                if not numeric:
                    continue
                preferred_key = str(preferred_aggregation or '').lower()
                if preferred_key and preferred_key in numeric:
                    aggregation = preferred_aggregation or preferred_key.title()
                else:
                    aggregation = 'Average' if 'average' in numeric else ('Maximum' if 'maximum' in numeric else 'Total')
                raw_value = float(numeric[str(aggregation).lower()])
                out.append({
                    'Timestamp': dp.get('timeStamp'),
                    'Value': raw_value * value_scale,
                    'Average': float(numeric['average']) if 'average' in numeric else None,
                    'Maximum': float(numeric['maximum']) if 'maximum' in numeric else None,
                    'Total': float(numeric['total']) if 'total' in numeric else None,
                    'Unit': output_unit or unit,
                    'MetricName': metric_name,
                    'Aggregation': aggregation,
                    'Granularity': interval,
                    'Interval': interval,
                })
    out.sort(key=lambda p: p.get('Timestamp') or '')
    return out, None


def add_nat_gateway_cost_justification(record: dict[str, Any], rid: str, token: str, from_date: str, to_date: str) -> None:
    if not is_nat_gateway_id(rid):
        return
    record.setdefault('Metrics', {})
    for category in NAT_GATEWAY_CATEGORY_ORDER:
        record['Metrics'].setdefault(category, [])
    for category, spec in NAT_GATEWAY_METRICS.items():
        points, err = query_metric(
            rid,
            spec['MetricName'],
            token,
            from_date,
            to_date,
            preferred_aggregation=spec['Aggregation'],
            output_unit=spec['Unit'],
            value_scale=float(spec.get('Scale') or 1.0),
        )
        if err:
            record.setdefault('MetricErrors', []).append({'MetricCategory': category, 'MetricName': spec['MetricName'], 'Error': err})
            continue
        for point in points:
            point['MetricCategory'] = category
        record['Metrics'][category].extend(points)


def is_azure_metric_retention_error(error: Any) -> bool:
    text = str(error or '').lower()
    return 'max metrics retention period' in text or 'out of the max metrics retention' in text


def azure_resource_record(resource: dict[str, Any], token: str, from_date: str, to_date: str, per_category: int) -> dict[str, Any]:
    rid = resource['ResourceID']
    record = {
        'ResourceID': rid,
        'ResourceType': resource.get('ResourceType'),
        'SubscriptionId': resource_subscription_id(rid),
        'CredentialProfile': resource.get('CredentialProfile'),
        'ConfiguredSubscriptionId': resource.get('ConfiguredSubscriptionId'),
        'HealthSource': 'AzureMonitor',
        'TemporalCoverage': 'HourlyAzureMonitorMetrics',
        'RequestedFromDate': from_date,
        'RequestedToDate': to_date,
        'Metrics': {category: [] for category in AZURE_CATEGORY_ORDER},
        'OverviewMetrics': {category: [] for category in AZURE_CATEGORY_ORDER},
        'MetricErrors': [],
    }
    if is_nat_gateway_id(rid):
        for category in NAT_GATEWAY_CATEGORY_ORDER:
            record['Metrics'][category] = []
            record['OverviewMetrics'][category] = []
    try:
        defs = metric_definitions(rid, token)
    except Exception as e:
        record['TemporalCoverage'] = 'AzureMetricDefinitionsUnavailable'
        record['MetricErrors'].append({'Stage': 'metricDefinitions', 'Error': f'{type(e).__name__}: {e}'})
        return record
    chosen = choose_azure_metrics(defs, per_category)
    for category in AZURE_CATEGORY_ORDER:
        for metric in chosen.get(category, []):
            points, err = query_metric(rid, metric['MetricName'], token, from_date, to_date)
            if err:
                record['MetricErrors'].append({'MetricCategory': category, 'MetricName': metric['MetricName'], 'Granularity': 'PT1H', 'Aggregation': 'Average,Maximum,Total', 'Error': err})
            else:
                # If more than one metric maps to a category, keep all points but preserve source metric name.
                record['Metrics'][category].extend(points)
            overview_points, overview_err = query_metric(
                rid,
                metric['MetricName'],
                token,
                from_date,
                to_date,
                preferred_aggregation='Average',
                interval='PT6H',
                aggregation_param='Average',
            )
            if overview_err:
                record['MetricErrors'].append({'MetricCategory': category, 'MetricName': metric['MetricName'], 'Granularity': 'PT6H', 'Aggregation': 'Average', 'Error': overview_err})
                continue
            record['OverviewMetrics'][category].extend(overview_points)
    add_nat_gateway_cost_justification(record, rid, token, from_date, to_date)
    if not any(record['Metrics'].values()) and record['MetricErrors']:
        if any(is_azure_metric_retention_error(e.get('Error')) for e in record['MetricErrors']):
            record['TemporalCoverage'] = 'AzureMetricRetentionExpired'
            record['CoverageNote'] = 'Requested date range is outside Azure Monitor platform metrics retention; hourly Azure metric points are unavailable and were not fabricated.'
        else:
            record['TemporalCoverage'] = 'AzureHourlyMetricsUnavailable'
    elif not any(record['Metrics'].values()):
        record['TemporalCoverage'] = 'NoMatchingAzureMetrics'
    return record


def load_snapshots(snapshot_file: Path = SNAPSHOT_FILE) -> list[dict[str, Any]]:
    if not snapshot_file.exists():
        return []
    rows: list[dict[str, Any]] = []
    with snapshot_file.open(encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def mongo_metrics_from_snapshots(snapshots: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    metrics: dict[str, list[dict[str, Any]]] = {category: [] for category in MONGO_METRIC_ORDER}
    for snap in sorted(snapshots, key=lambda r: r.get('Hour') or r.get('Timestamp') or ''):
        ts = snap.get('Hour') or snap.get('Timestamp')
        raw = snap.get('Metrics') or {}
        def add(category: str, metric_name: str, value: Any, unit: str = 'Count') -> None:
            if value is None:
                return
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                return
            metrics[category].append({'Timestamp': ts, 'Value': numeric, 'Unit': unit, 'MetricName': metric_name, 'Aggregation': 'PointInTimeHourlySnapshot'})
        storage = raw.get('dbStats.total.storageSize') or {}
        add('StorageSize', 'dbStats.total.storageSize', storage.get('Value'), storage.get('Unit') or 'Bytes')
        index = raw.get('dbStats.total.indexSize') or {}
        add('IndexSize', 'dbStats.total.indexSize', index.get('Value'), index.get('Unit') or 'Bytes')
        long_running = raw.get('currentOp.longRunningOperations') or {}
        add('LongRunningSlowQueries', 'currentOp.longRunningOperations', long_running.get('Value'), long_running.get('Unit') or 'Count')
        conn = raw.get('connections.current') or {}
        add('Connections', 'connections.current', conn.get('Value'), conn.get('Unit') or 'Count')
        op_total = 0.0
        has_op = False
        for name, metric in raw.items():
            if str(name).startswith('opcounters.') and isinstance(metric, dict):
                try:
                    op_total += float(metric.get('Value'))
                    has_op = True
                except (TypeError, ValueError):
                    pass
        if has_op:
            add('IOPs', 'opcounters.total', op_total, 'Count')
    return metrics


def generate_mongo_records(resources: list[dict[str, Any]], creds: dict[str, Any], from_date: str, to_date: str, snapshot_file: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    start = datetime.fromisoformat(from_date).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(to_date).replace(tzinfo=timezone.utc) + timedelta(days=1)
    all_snapshots = load_snapshots(snapshot_file)
    by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for snap in all_snapshots:
        key = snap.get('MongoDBResource')
        ts_s = snap.get('Timestamp')
        if not key or not ts_s:
            continue
        try:
            ts = datetime.fromisoformat(str(ts_s).replace('Z', '+00:00'))
        except ValueError:
            continue
        if start <= ts < end:
            by_key[key].append(snap)
    records: list[dict[str, Any]] = []
    for resource in resources:
        rid = resource['ResourceID']
        key, _uri = resolve_mongodb_credential_key(creds, rid)
        if not key:
            continue
        snaps = by_key.get(key, [])
        metrics = mongo_metrics_from_snapshots(snaps)
        records.append({
            'ResourceID': rid,
            'ResourceType': resource.get('ResourceType'),
            'MongoDBResource': key,
            'HealthSource': 'MongoDBCommandsOnly',
            'TemporalCoverage': 'HourlySnapshotsAvailable' if any(metrics.values()) else 'HourlySnapshotsUnavailable',
            'RequestedFromDate': from_date,
            'RequestedToDate': to_date,
            'SnapshotCount': len(snaps),
            'Metrics': metrics,
            'CoverageNote': None if snaps else 'No command-derived hourly MongoDB snapshots were stored for this resource in the requested date range; values were not fabricated or backfilled from cached health collections.',
        })
    return records, {'stored_snapshot_count_in_range': sum(len(v) for v in by_key.values()), 'mapped_mongo_resource_count': len(records)}


def generate(data_dir: Path, run_id: str, password: str, azure_workers: int, per_category: int, snapshot_file: Path, skip_azure: bool = False, skip_mongo: bool = False) -> dict[str, Any]:
    run = dashboard_api.get_run(data_dir, run_id)
    resolved = run['run_id']
    summary = dashboard_api.run_summary(data_dir, resolved)
    from_date = summary.get('fromDate')
    to_date = summary.get('toDate')
    if not from_date or not to_date:
        raise RuntimeError(f'Run {resolved} does not include fromDate/toDate.')
    resources = dashboard_api.affected_resources(data_dir, resolved)
    creds = decrypt_credentials(password)
    azure_records: list[dict[str, Any]] = []
    azure_error = None
    if not skip_azure:
        arm_resources = [r for r in resources if is_arm_id(r.get('ResourceID'))]
        profiles = azure_credential_profiles(creds)
        profile_by_sub = {p['subscription_id'].lower(): p for p in profiles}
        query_groups: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
        available_subscriptions = sorted(p['subscription_id'] for p in profiles)
        for r in arm_resources:
            rid = r.get('ResourceID')
            rid_sub = resource_subscription_id(rid)
            profile = profile_by_sub.get(str(rid_sub or '').lower())
            if not profile:
                azure_records.append({
                    'ResourceID': rid,
                    'ResourceType': r.get('ResourceType'),
                    'SubscriptionId': rid_sub,
                    'ConfiguredSubscriptionIds': available_subscriptions,
                    'HealthSource': 'AzureMonitor',
                    'TemporalCoverage': 'AzureSubscriptionNotConfigured',
                    'RequestedFromDate': from_date,
                    'RequestedToDate': to_date,
                    'Metrics': {category: [] for category in (AZURE_CATEGORY_ORDER + (NAT_GATEWAY_CATEGORY_ORDER if is_nat_gateway_id(rid) else []))},
                    'OverviewMetrics': {category: [] for category in (AZURE_CATEGORY_ORDER + (NAT_GATEWAY_CATEGORY_ORDER if is_nat_gateway_id(rid) else []))},
                    'MetricErrors': [{'Stage': 'subscription', 'Error': 'No Azure credential profile is configured for this resource subscription; Azure health lookup skipped.'}],
                })
                continue
            enriched = dict(r)
            enriched['CredentialProfile'] = profile['ProfileName']
            enriched['ConfiguredSubscriptionId'] = profile['subscription_id']
            query_groups.setdefault(profile['ProfileName'], (profile, []))[1].append(enriched)
        if not profiles and arm_resources:
            azure_error = 'No Azure credential profiles are configured.'
        for profile, query_resources in query_groups.values():
            try:
                token = get_token(profile)
                with ThreadPoolExecutor(max_workers=azure_workers) as pool:
                    futures = {pool.submit(azure_resource_record, r, token, from_date, to_date, per_category): r for r in query_resources}
                    for future in as_completed(futures):
                        try:
                            azure_records.append(future.result())
                        except Exception as e:
                            r = futures[future]
                            azure_records.append({
                                'ResourceID': r.get('ResourceID'),
                                'ResourceType': r.get('ResourceType'),
                                'SubscriptionId': resource_subscription_id(r.get('ResourceID')),
                                'CredentialProfile': r.get('CredentialProfile'),
                                'ConfiguredSubscriptionId': r.get('ConfiguredSubscriptionId'),
                                'HealthSource': 'AzureMonitor',
                                'TemporalCoverage': 'AzureHourlyMetricsUnavailable',
                                'RequestedFromDate': from_date,
                                'RequestedToDate': to_date,
                                'Metrics': {category: [] for category in AZURE_CATEGORY_ORDER},
                                'OverviewMetrics': {category: [] for category in AZURE_CATEGORY_ORDER},
                                'MetricErrors': [{'Stage': 'resource', 'Error': f'{type(e).__name__}: {e}'}],
                            })
            except Exception as e:
                profile_error = f'{type(e).__name__}: {e}'
                azure_error = profile_error if azure_error is None else f'{azure_error}; {profile["ProfileName"]}: {profile_error}'
                for r in query_resources:
                    azure_records.append({
                        'ResourceID': r.get('ResourceID'),
                        'ResourceType': r.get('ResourceType'),
                        'SubscriptionId': resource_subscription_id(r.get('ResourceID')),
                        'CredentialProfile': r.get('CredentialProfile'),
                        'ConfiguredSubscriptionId': r.get('ConfiguredSubscriptionId'),
                        'HealthSource': 'AzureMonitor',
                        'TemporalCoverage': 'AzureAuthenticationUnavailable',
                        'RequestedFromDate': from_date,
                        'RequestedToDate': to_date,
                        'Metrics': {category: [] for category in AZURE_CATEGORY_ORDER},
                        'OverviewMetrics': {category: [] for category in AZURE_CATEGORY_ORDER},
                        'MetricErrors': [{'Stage': 'auth', 'Error': profile_error}],
                    })
    azure_records.sort(key=lambda r: r.get('ResourceID') or '')

    mongo_records: list[dict[str, Any]] = []
    mongo_meta: dict[str, Any] = {}
    if not skip_mongo:
        mongo_records, mongo_meta = generate_mongo_records(resources, creds, from_date, to_date, snapshot_file)
        mongo_records.sort(key=lambda r: r.get('ResourceID') or '')

    azure_path = data_dir / f'Azure_Health_Analysis_{resolved}.json'
    mongo_path = data_dir / f'Mongo_Health_Analysis_{mongo_run_id_from_run_id(resolved)}.json'
    azure_path.write_text(json.dumps(azure_records, indent=2, default=str), encoding='utf-8')
    if not skip_mongo:
        mongo_path.write_text(json.dumps(mongo_records, indent=2, default=str), encoding='utf-8')
    elif mongo_path.exists():
        existing_mongo = dashboard_api.read_json(mongo_path)
        if isinstance(existing_mongo, list):
            mongo_records = existing_mongo
            mongo_meta = {'stored_snapshot_count_in_range': sum(len(v) for r in mongo_records for v in (r.get('Metrics') or {}).values() if isinstance(v, list))}

    summary_path = run['files'].get('summary')
    if summary_path and Path(summary_path).exists():
        data = dashboard_api.read_json(summary_path)
        data['azure_health_analysis_file'] = str(azure_path)
        data['mongo_health_analysis_file'] = str(mongo_path)
        data['azure_health_resource_count'] = len(azure_records)
        data['azure_health_resources_with_hourly_points'] = sum(1 for r in azure_records if any((r.get('Metrics') or {}).values()))
        data['azure_health_resources_with_pt6h_overview_points'] = sum(1 for r in azure_records if any((r.get('OverviewMetrics') or {}).values()))
        data['azure_health_metric_error_count'] = sum(len(r.get('MetricErrors') or []) for r in azure_records)
        data['mongo_health_resource_count'] = len(mongo_records)
        data['mongo_health_resources_with_hourly_points'] = sum(1 for r in mongo_records if any((r.get('Metrics') or {}).values()))
        data['mongo_health_snapshot_count'] = mongo_meta.get('stored_snapshot_count_in_range', 0)
        Path(summary_path).write_text(json.dumps(data, indent=2), encoding='utf-8')

    return {
        'ok': True,
        'run_id': resolved,
        'fromDate': from_date,
        'toDate': to_date,
        'azure_health_analysis_file': str(azure_path),
        'azure_resource_count': len(azure_records),
        'azure_resources_with_hourly_points': sum(1 for r in azure_records if any((r.get('Metrics') or {}).values())),
        'azure_resources_with_pt6h_overview_points': sum(1 for r in azure_records if any((r.get('OverviewMetrics') or {}).values())),
        'azure_metric_error_count': sum(len(r.get('MetricErrors') or []) for r in azure_records),
        'azure_error': azure_error,
        'mongo_health_analysis_file': str(mongo_path),
        'mongo_resource_count': len(mongo_records),
        'mongo_resources_with_hourly_points': sum(1 for r in mongo_records if any((r.get('Metrics') or {}).values())),
        'mongo_snapshot_count': mongo_meta.get('stored_snapshot_count_in_range', 0),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description='Generate separate Azure and MongoDB health analysis files for CloudVitals drilldowns.')
    parser.add_argument('--data-dir', default='/opt/data')
    parser.add_argument('--run-id', default='latest')
    parser.add_argument('--azure-workers', type=int, default=8)
    parser.add_argument('--max-metrics-per-category', type=int, default=1)
    parser.add_argument('--snapshot-file', default=str(SNAPSHOT_FILE))
    parser.add_argument('--skip-azure', action='store_true')
    parser.add_argument('--skip-mongo', action='store_true')
    args = parser.parse_args()
    password = os.environ.get('COST_HEALTH_PWD') or getpass.getpass('Credential password: ')
    result = generate(Path(args.data_dir), args.run_id, password, args.azure_workers, args.max_metrics_per_category, Path(args.snapshot_file), args.skip_azure, args.skip_mongo)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
