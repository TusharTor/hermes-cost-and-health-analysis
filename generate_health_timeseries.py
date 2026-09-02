#!/usr/bin/env python3
"""Generate Health-Timeseries_<run_id>.json for dashboard drilldowns.

This script extends the Cost and Health Analysis Agent output with hourly Azure
Monitor datapoints for affected resource anomaly dates. It never writes secrets.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import stat
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import dashboard_api

CRED_PATH = Path('/opt/data/azure_credentials/azure_management.json.enc')
MGMT = 'https://management.azure.com'
SEV_RANK = dashboard_api.SEVERITY_RANK
CATEGORY_PATTERNS = {
    'CPU': ['percentagecpu', 'cpu', 'cputime'],
    'Memory': ['memory', 'workingset'],
    'DiskUsage': ['filesystem', 'usedcapacity', 'diskused', 'storage'],
    'DiskIO': ['iops', 'diskread', 'diskwrite', 'transactions'],
    'NetworkTraffic': ['network', 'ingress', 'egress', 'bytesreceived', 'bytessent'],
    'Errors': ['errors', 'failed', '5xx', 'throttl'],
    'Availability': ['availability', 'healthcheck']
}


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


def http_json(method: str, url: str, headers: dict[str, str] | None = None, data: dict[str, str] | None = None, timeout: int = 30) -> dict[str, Any]:
    encoded = urlencode(data).encode('utf-8') if data else None
    req = Request(url, data=encoded, method=method, headers=headers or {})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')[:500]
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
    })
    token = payload.get('access_token')
    if not token:
        raise RuntimeError('Token response did not contain access_token.')
    return token


def is_arm_id(rid: str) -> bool:
    return isinstance(rid, str) and rid.lower().startswith('/subscriptions/') and '/providers/' in rid.lower()


def normalized_metric_name(name: str) -> str:
    return ''.join(ch for ch in name.lower() if ch.isalnum())


def metric_category(name: str) -> str | None:
    n = normalized_metric_name(name)
    for cat, pats in CATEGORY_PATTERNS.items():
        if any(p in n for p in pats):
            return cat
    return None


def metric_definitions(resource_id: str, token: str) -> list[dict[str, Any]]:
    url = f'{MGMT}{resource_id}/providers/microsoft.insights/metricDefinitions?api-version=2018-01-01'
    return http_json('GET', url, headers={'Authorization': f'Bearer {token}'}, timeout=30).get('value', [])


def choose_metrics(defs: list[dict[str, Any]], max_metrics: int) -> list[dict[str, str]]:
    selected = []
    seen_categories = set()
    for item in defs:
        obj = item.get('name') or {}
        name = obj.get('value') or obj.get('localizedValue')
        if not isinstance(name, str):
            continue
        category = metric_category(name)
        if not category:
            continue
        unit = item.get('unit') or item.get('primaryAggregationType') or 'Unknown'
        # Favor broad category coverage first, then fill with additional metrics.
        priority = 0 if category not in seen_categories else 1
        selected.append({'MetricName': name, 'MetricCategory': category, 'Unit': unit, '_priority': priority})
        seen_categories.add(category)
    selected.sort(key=lambda x: (x['_priority'], list(CATEGORY_PATTERNS).index(x['MetricCategory'])))
    out = []
    used = set()
    for row in selected:
        if row['MetricName'] in used:
            continue
        used.add(row['MetricName'])
        row.pop('_priority', None)
        out.append(row)
        if len(out) >= max_metrics:
            break
    return out


def query_hourly(resource_id: str, metric_name: str, token: str, date: str) -> tuple[list[dict[str, Any]], str | None]:
    start = f'{date}T00:00:00Z'
    end = (datetime.fromisoformat(date).replace(tzinfo=timezone.utc) + timedelta(days=1)).strftime('%Y-%m-%dT00:00:00Z')
    params = urlencode({
        'api-version': '2018-01-01', 'timespan': f'{start}/{end}', 'interval': 'PT1H',
        'metricnames': metric_name, 'aggregation': 'Average,Maximum,Total'
    }, quote_via=quote)
    url = f'{MGMT}{resource_id}/providers/microsoft.insights/metrics?{params}'
    try:
        data = http_json('GET', url, headers={'Authorization': f'Bearer {token}'}, timeout=45)
    except Exception as e:
        return [], str(e)
    points = []
    for item in data.get('value', []):
        unit = item.get('unit')
        for ts in item.get('timeseries', []):
            for dp in ts.get('data', []):
                value = None
                aggregation = None
                for key in ['average', 'maximum', 'total']:
                    if dp.get(key) is not None:
                        value = float(dp[key])
                        aggregation = key.capitalize()
                        break
                if value is not None:
                    points.append({'Timestamp': dp.get('timeStamp'), 'Value': value, 'Aggregation': aggregation, 'Unit': unit})
    points.sort(key=lambda p: p.get('Timestamp') or '')
    return points, None


def planned_resource_days(data_dir: Path, run_id: str, limit: int) -> list[tuple[str, str]]:
    resources = dashboard_api.affected_resources(data_dir, run_id)
    cost_by_rid = {}
    for res in resources:
        try:
            rows = dashboard_api.cost_timeseries(data_dir, run_id, res['ResourceID'])
        except Exception:
            continue
        candidates = [r for r in rows if r.get('IsAnomaly')]
        if not candidates:
            candidates = rows
        top = sorted(candidates, key=lambda r: (SEV_RANK.get(r.get('Severity'), 0), abs(r.get('DeviationPercentage') or 0), r.get('CostAmount') or 0), reverse=True)[0]
        cost_by_rid[res['ResourceID']] = top['AnalysisDate']
    pairs = [(r['ResourceID'], cost_by_rid[r['ResourceID']]) for r in resources if r['ResourceID'] in cost_by_rid and is_arm_id(r['ResourceID'])]
    return pairs[:limit]


def generate(data_dir: Path, run_id: str, limit: int, max_metrics: int, password: str) -> dict[str, Any]:
    creds = decrypt_credentials(password)
    token = get_token(creds)
    series = []
    errors = []
    resources = dashboard_api.affected_resources(data_dir, run_id)
    resource_type_by_id = {r['ResourceID']: r.get('ResourceType') for r in resources}
    for rid, date in planned_resource_days(data_dir, run_id, limit):
        try:
            defs = metric_definitions(rid, token)
            metrics = choose_metrics(defs, max_metrics)
            for m in metrics:
                points, err = query_hourly(rid, m['MetricName'], token, date)
                if err:
                    errors.append({'ResourceID': rid, 'Date': date, 'MetricName': m['MetricName'], 'Error': err})
                    continue
                series.append({
                    'ResourceID': rid,
                    'ResourceType': resource_type_by_id.get(rid),
                    'Date': date,
                    'MetricCategory': m['MetricCategory'],
                    'MetricName': m['MetricName'],
                    'Unit': points[0].get('Unit') if points else m.get('Unit'),
                    'Aggregation': points[0].get('Aggregation') if points else 'Average/Maximum/Total',
                    'Points': [{'Timestamp': p['Timestamp'], 'Value': p['Value']} for p in points]
                })
        except Exception as e:
            errors.append({'ResourceID': rid, 'Date': date, 'Error': f'{type(e).__name__}: {e}'})
    out_path = data_dir / f'Health-Timeseries_{run_id}.json'
    err_path = data_dir / f'Health-Timeseries_{run_id}.errors.json'
    out_path.write_text(json.dumps(series, indent=2), encoding='utf-8')
    err_path.write_text(json.dumps(errors, indent=2), encoding='utf-8')
    # Update summary when available so dashboard auto-detects the file.
    run = dashboard_api.get_run(data_dir, run_id)
    summary_path = run['files'].get('summary')
    if summary_path and Path(summary_path).exists():
        summary = dashboard_api.read_json(summary_path)
        summary['health_timeseries_file'] = str(out_path)
        summary['health_timeseries_error_file'] = str(err_path)
        summary['health_timeseries_series_count'] = len(series)
        summary['health_timeseries_error_count'] = len(errors)
        Path(summary_path).write_text(json.dumps(summary, indent=2), encoding='utf-8')
    return {'ok': True, 'run_id': run_id, 'health_timeseries_file': str(out_path), 'health_timeseries_error_file': str(err_path), 'series_count': len(series), 'error_count': len(errors), 'errors': errors[:20]}


def main() -> int:
    parser = argparse.ArgumentParser(description='Generate hourly health time-series JSON for dashboard drilldowns.')
    parser.add_argument('--data-dir', default='/opt/data')
    parser.add_argument('--run-id', default='latest')
    parser.add_argument('--limit', type=int, default=20, help='Top affected ARM resources to query')
    parser.add_argument('--max-metrics', type=int, default=8, help='Max Azure metrics per resource/date')
    parser.add_argument('--dry-run', action='store_true', help='Only print planned resource/date queries; no credentials needed')
    args = parser.parse_args()
    data_dir = Path(args.data_dir)
    run = dashboard_api.get_run(data_dir, args.run_id)
    run_id = run['run_id']
    pairs = planned_resource_days(data_dir, run_id, args.limit)
    if args.dry_run:
        print(json.dumps({'ok': True, 'run_id': run_id, 'planned_resource_days': len(pairs), 'pairs': [{'ResourceID': r, 'Date': d} for r, d in pairs[:20]]}, indent=2))
        return 0
    password = os.environ.get('COST_HEALTH_PWD') or getpass.getpass('Credential password: ')
    print(json.dumps(generate(data_dir, run_id, args.limit, args.max_metrics, password), indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
