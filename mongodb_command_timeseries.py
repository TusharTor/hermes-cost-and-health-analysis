#!/usr/bin/env python3
"""Collect/build MongoDB command-derived hourly health time series.

This module supports the Cost + Health dashboard drilldown requirement for
MongoDB resources. It never reads cached health collections such as
MongoAtlasCronJob/HealthSummaries. It samples MongoDB point-in-time commands and
stores sanitized numeric metrics for dashboard graphs.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pymongo import MongoClient

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path('/opt/data')
SNAPSHOT_FILE = DATA_DIR / 'MongoDB-Command-Snapshots.jsonl'
CRED_PATH = Path('/opt/data/azure_credentials/azure_management.json.enc')
sys.path.insert(0, str(ROOT))
sys.path.insert(0, '/opt/data')
import dashboard_api  # noqa: E402
from cost_health_analysis_health import resolve_mongodb_credential_key  # noqa: E402

GENERIC_MONGO_KEYS = {
    'mongo_connection_string', 'mongodb_connection_string', 'mongoconnectionstring',
    'mongodb_uri', 'mongo_uri', 'connection_string', 'mongo_db_connection_string'
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


def is_mongo_uri(value: Any) -> bool:
    return isinstance(value, str) and value.strip().startswith(('mongodb://', 'mongodb+srv://'))


def discover_mongo_targets(creds: dict[str, Any], only_keys: list[str] | None = None) -> list[tuple[str, str]]:
    targets: list[tuple[str, str]] = []
    wanted = {k.lower() for k in only_keys or []}
    for key, value in creds.items():
        key_s = str(key)
        if not is_mongo_uri(value):
            continue
        if wanted and key_s.lower() not in wanted:
            continue
        # By default do not sample the app/cost DB connection string; only named
        # workload MongoDB credentials such as Platform_MongoDB/Piaggio_MongoDB.
        if not wanted and key_s.lower() in GENERIC_MONGO_KEYS:
            continue
        if not wanted and 'mongo' not in key_s.lower():
            continue
        targets.append((key_s, value.strip()))
    return sorted(targets, key=lambda kv: kv[0].lower())


def command_summary(client: MongoClient) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    out: dict[str, Any] = {}
    try:
        status = client.admin.command('serverStatus')
        out['serverStatus'] = {
            'connections': status.get('connections') or {},
            'network': status.get('network') or {},
            'opcounters': status.get('opcounters') or {},
            'mem': status.get('mem') or {},
            'uptime': status.get('uptime'),
            'version': status.get('version'),
            'ok': status.get('ok'),
        }
    except Exception as e:
        errors.append(f'serverStatus:{type(e).__name__}')
    try:
        host = client.admin.command('hostInfo')
        out['hostInfo'] = {
            'system': {k: (host.get('system') or {}).get(k) for k in ['numCores', 'memSizeMB', 'cpuArch'] if k in (host.get('system') or {})},
            'os': {k: (host.get('os') or {}).get(k) for k in ['type', 'name', 'version'] if k in (host.get('os') or {})},
            'ok': host.get('ok'),
        }
    except Exception as e:
        errors.append(f'hostInfo:{type(e).__name__}')
    db_names: list[str] = []
    try:
        db_names = [n for n in client.list_database_names() if n not in {'admin', 'local', 'config'}]
        out['databaseNames'] = db_names
    except Exception as e:
        errors.append(f'list_database_names:{type(e).__name__}')
        out['databaseNames'] = []
    db_stats: dict[str, Any] = {}
    for db_name in db_names[:20]:
        try:
            st = client[db_name].command('dbStats')
            db_stats[db_name] = {k: st.get(k) for k in ['collections', 'objects', 'avgObjSize', 'dataSize', 'storageSize', 'indexes', 'indexSize', 'ok'] if k in st}
        except Exception as e:
            db_stats[db_name] = {'error': type(e).__name__}
    out['dbStats'] = db_stats
    try:
        cur = client.admin.command('currentOp', allUsers=True, idleConnections=False)
        ops = cur.get('inprog') or []
        active = [o for o in ops if o.get('active')]
        long_running = [o for o in active if (o.get('secs_running') or o.get('microsecs_running', 0) / 1_000_000) >= 60]
        out['currentOp'] = {'activeOperations': len(active), 'longRunningOperations': len(long_running), 'returnedOperations': len(ops), 'ok': cur.get('ok')}
    except Exception as e:
        errors.append(f'currentOp:{type(e).__name__}')
    out['slowQueries'] = {'Status': 'Not Checked', 'Reason': 'User requested command data only; profiler/system.profile collection reads are not used.'}
    return out, errors


def flatten_metrics(summary: dict[str, Any]) -> dict[str, tuple[float, str, str]]:
    """Return metric name -> (value, unit, category)."""
    metrics: dict[str, tuple[float, str, str]] = {}

    def add(name: str, value: Any, unit: str, category: str) -> None:
        if isinstance(value, bool) or value is None:
            return
        try:
            metrics[name] = (float(value), unit, category)
        except (TypeError, ValueError):
            return

    ss = summary.get('serverStatus') or {}
    for k, v in (ss.get('connections') or {}).items():
        add(f'connections.{k}', v, 'Count', 'MongoDBConnections')
    for k, v in (ss.get('network') or {}).items():
        unit = 'Bytes' if 'bytes' in k.lower() else 'Count'
        add(f'network.{k}', v, unit, 'MongoDBNetwork')
    for k, v in (ss.get('opcounters') or {}).items():
        add(f'opcounters.{k}', v, 'Count', 'MongoDBOperations')
    for k, v in (ss.get('mem') or {}).items():
        unit = 'MiB' if k in {'resident', 'virtual', 'mapped'} else 'Count'
        add(f'mem.{k}', v, unit, 'MongoDBMemory')
    cur = summary.get('currentOp') or {}
    for k in ['activeOperations', 'longRunningOperations', 'returnedOperations']:
        add(f'currentOp.{k}', cur.get(k), 'Count', 'MongoDBOperations')
    totals = defaultdict(float)
    for st in (summary.get('dbStats') or {}).values():
        if not isinstance(st, dict) or st.get('error'):
            continue
        for key in ['objects', 'dataSize', 'storageSize', 'indexes', 'indexSize']:
            val = st.get(key)
            if isinstance(val, (int, float)):
                totals[key] += float(val)
    for key, value in totals.items():
        unit = 'Bytes' if key.lower().endswith('size') else 'Count'
        add(f'dbStats.total.{key}', value, unit, 'MongoDBStorage')
    return metrics


def collect(password: str, keys: list[str] | None = None, snapshot_file: Path = SNAPSHOT_FILE) -> dict[str, Any]:
    creds = decrypt_credentials(password)
    targets = discover_mongo_targets(creds, keys)
    timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    snapshot_file.parent.mkdir(parents=True, exist_ok=True)
    records = []
    for key, uri in targets:
        record = {
            'Timestamp': timestamp,
            'Date': timestamp[:10],
            'Hour': timestamp[:13] + ':00:00Z',
            'MongoDBResource': key,
            'HealthSource': 'MongoDBCommandsOnly',
        }
        try:
            client = MongoClient(uri, serverSelectionTimeoutMS=15000)
            client.admin.command('ping')
            summary, errors = command_summary(client)
            record['CommandSummary'] = summary
            record['Metrics'] = {name: {'Value': value, 'Unit': unit, 'MetricCategory': cat} for name, (value, unit, cat) in flatten_metrics(summary).items()}
            record['CommandErrors'] = errors
            record['MongoDBStatus'] = 'Healthy' if not errors else 'Insufficient Data'
            record['MongoDBHealthReason'] = 'MongoDB command snapshot collected successfully.' if not errors else 'Some MongoDB commands failed; see CommandErrors.'
        except Exception as e:
            record['CommandSummary'] = {}
            record['Metrics'] = {}
            record['CommandErrors'] = [f'connection:{type(e).__name__}']
            record['MongoDBStatus'] = 'Unavailable'
            record['MongoDBHealthReason'] = 'Unable to connect to configured MongoDB cluster or run command checks.'
        records.append(record)
    with snapshot_file.open('a', encoding='utf-8') as fh:
        for record in records:
            fh.write(json.dumps(record, separators=(',', ':'), default=str) + '\n')
    return {'ok': True, 'snapshot_file': str(snapshot_file), 'timestamp': timestamp, 'targets_checked': len(targets), 'records_appended': len(records), 'target_keys': [k for k, _ in targets]}


def load_snapshots(snapshot_file: Path) -> list[dict[str, Any]]:
    if not snapshot_file.exists():
        return []
    rows = []
    with snapshot_file.open(encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def build_run(password: str, run_id: str, data_dir: Path = DATA_DIR, snapshot_file: Path = SNAPSHOT_FILE) -> dict[str, Any]:
    creds = decrypt_credentials(password)
    run = dashboard_api.get_run(data_dir, run_id)
    resolved = run['run_id']
    from_date = run.get('fromDate') or (run.get('summary') or {}).get('fromDate')
    to_date = run.get('toDate') or (run.get('summary') or {}).get('toDate')
    if not from_date or not to_date:
        raise RuntimeError(f'Run {resolved} does not include fromDate/toDate.')
    start = datetime.fromisoformat(from_date).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(to_date).replace(tzinfo=timezone.utc) + timedelta(days=1)
    resources = dashboard_api.affected_resources(data_dir, resolved)
    rid_to_key: dict[str, str] = {}
    for res in resources:
        key, _uri = resolve_mongodb_credential_key(creds, res['ResourceID'])
        if key:
            rid_to_key[res['ResourceID']] = key
    snapshots = []
    wanted_keys = set(rid_to_key.values())
    for row in load_snapshots(snapshot_file):
        key = row.get('MongoDBResource')
        ts_s = row.get('Timestamp')
        if key not in wanted_keys or not ts_s:
            continue
        try:
            ts = datetime.fromisoformat(ts_s.replace('Z', '+00:00'))
        except ValueError:
            continue
        if start <= ts < end:
            snapshots.append(row)
    series_map: dict[tuple[str, str, str], dict[str, Any]] = {}
    key_to_rids = defaultdict(list)
    for rid, key in rid_to_key.items():
        key_to_rids[key].append(rid)
    for snap in snapshots:
        key = snap.get('MongoDBResource')
        for rid in key_to_rids.get(key, []):
            for metric_name, metric in (snap.get('Metrics') or {}).items():
                date = str(snap.get('Date') or snap.get('Timestamp', '')[:10])
                map_key = (rid, date, metric_name)
                item = series_map.setdefault(map_key, {
                    'ResourceID': rid,
                    'ResourceType': next((r.get('ResourceType') for r in resources if r['ResourceID'] == rid), None),
                    'Date': date,
                    'MetricCategory': metric.get('MetricCategory') or 'MongoDBCommand',
                    'MetricName': metric_name,
                    'Unit': metric.get('Unit') or 'Unknown',
                    'Aggregation': 'PointInTimeHourlySnapshot',
                    'HealthSource': 'MongoDBCommandsOnly',
                    'MongoDBResource': key,
                    'Points': [],
                })
                item['Points'].append({'Timestamp': snap.get('Hour') or snap.get('Timestamp'), 'Value': metric.get('Value')})
    mongo_series = list(series_map.values())
    for item in mongo_series:
        seen = {}
        for point in item['Points']:
            seen[point['Timestamp']] = point
        item['Points'] = [seen[t] for t in sorted(seen)]
    out_path = data_dir / f'Health-Timeseries_{resolved}.json'
    existing = []
    if out_path.exists():
        existing = json.loads(out_path.read_text(encoding='utf-8'))
    non_mongo = [s for s in existing if not str(s.get('MetricCategory', '')).startswith('MongoDB') and s.get('HealthSource') != 'MongoDBCommandsOnly']
    combined = non_mongo + sorted(mongo_series, key=lambda s: (s['ResourceID'], s['Date'], s['MetricName']))
    out_path.write_text(json.dumps(combined, indent=2), encoding='utf-8')
    summary_path = run['files'].get('summary')
    if summary_path and Path(summary_path).exists():
        summary = dashboard_api.read_json(summary_path)
        summary['health_timeseries_file'] = str(out_path)
        summary['mongodb_command_snapshot_file'] = str(snapshot_file)
        summary['mongodb_health_timeseries_series_count'] = len(mongo_series)
        summary['mongodb_health_timeseries_resource_count'] = len(rid_to_key)
        summary['mongodb_health_timeseries_snapshot_count'] = len(snapshots)
        summary['mongodb_health_timeseries_temporal_coverage'] = 'HourlySnapshotsAvailable' if mongo_series else 'HourlySnapshotsUnavailable'
        summary['health_timeseries_series_count'] = len(combined)
        Path(summary_path).write_text(json.dumps(summary, indent=2), encoding='utf-8')
    return {'ok': True, 'run_id': resolved, 'health_timeseries_file': str(out_path), 'mongo_resource_count': len(rid_to_key), 'snapshot_count': len(snapshots), 'mongo_series_count': len(mongo_series), 'total_series_count': len(combined)}


def main() -> int:
    parser = argparse.ArgumentParser(description='MongoDB command-derived hourly health time-series helper')
    sub = parser.add_subparsers(dest='cmd', required=True)
    p_collect = sub.add_parser('collect', help='Collect one point-in-time command snapshot for configured MongoDB resources')
    p_collect.add_argument('--key', action='append', dest='keys', help='Credential key to sample; repeatable. Defaults to all named MongoDB workload keys.')
    p_collect.add_argument('--snapshot-file', default=str(SNAPSHOT_FILE))
    p_build = sub.add_parser('build-run', help='Build dashboard Health-Timeseries_<run_id>.json from stored command snapshots')
    p_build.add_argument('--run-id', default='latest')
    p_build.add_argument('--data-dir', default=str(DATA_DIR))
    p_build.add_argument('--snapshot-file', default=str(SNAPSHOT_FILE))
    args = parser.parse_args()
    password = os.environ.get('COST_HEALTH_PWD')
    if not password:
        import getpass
        password = getpass.getpass('Credential password: ')
    if args.cmd == 'collect':
        result = collect(password, args.keys, Path(args.snapshot_file))
    else:
        result = build_run(password, args.run_id, Path(args.data_dir), Path(args.snapshot_file))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
