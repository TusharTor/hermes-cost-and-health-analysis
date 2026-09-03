#!/usr/bin/env python3
"""Cronicle Collector/Analyzer integration for CloudVitals.

This module implements the Ops-Agent Cronicle correlation contract:
- collect Cronicle get_history/v1 rows into SQLite without secrets;
- deterministically analyze jobs overlapping an hourly anomaly window;
- expose only minimal dashboard-facing fields: scheduled_job, python_script;
- enrich Platform MongoDB hourly health points with those minimal results.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sqlite3
import ssl
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

DEFAULT_DATA_DIR = Path("/opt/data")
DEFAULT_DB_PATH = DEFAULT_DATA_DIR / "cronicle_history.sqlite3"
DEFAULT_CPU_THRESHOLD = 50.0
DEFAULT_WINDOW_MINUTES = 30
DEFAULT_MAX_RESULTS = 10
PLATFORM_MONGO_COST_IDS = {"Platform_MongoDb"}
PLATFORM_MONGO_HEALTH_IDS = {"Cluster_Platform"}

SCRIPT_RE = re.compile(r"(?i)(?:^|[\s=/:'\"`])([A-Za-z0-9_.-]+\.py)(?:$|[\s'\"`;,&|])")
SECRET_MARKERS = ("CRONICLE_KEY", "x-api-key", "Bearer ", "client_secret", "mongodb://", "mongodb+srv://")


def parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def epoch_seconds(dt: datetime) -> float:
    return dt.astimezone(timezone.utc).timestamp()


def safe_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def safe_int(value: Any) -> int | None:
    v = safe_float(value)
    return int(v) if v is not None else None


def _looks_secret_text(text: str) -> bool:
    return any(marker in text for marker in SECRET_MARKERS)


def _walk_strings(obj: Any):
    if isinstance(obj, str):
        if not _looks_secret_text(obj):
            yield obj
    elif isinstance(obj, dict):
        for key, value in obj.items():
            if str(key).lower() in {"key", "api_key", "password", "secret", "token", "clientsecret", "client_secret"}:
                continue
            yield from _walk_strings(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk_strings(value)


def extract_python_script(job: dict[str, Any]) -> str | None:
    """Return a script basename only when literal .py metadata exists.

    The Analyzer must not infer script names from job titles or database/resource
    names. A value is accepted only if actual Cronicle metadata/config contains a
    literal ``*.py`` token.
    """
    priority_keys = [
        "python_script", "pythonScript", "script", "script_name", "scriptName",
        "command", "command_line", "commandLine", "cmd", "params", "config", "event_config",
    ]
    for key in priority_keys:
        if key in job:
            for text in _walk_strings(job[key]):
                match = SCRIPT_RE.search(text)
                if match:
                    return Path(match.group(1)).name
    # Fallback to any non-secret string in the Cronicle row, still requiring a literal .py token.
    for text in _walk_strings(job):
        match = SCRIPT_RE.search(text)
        if match:
            return Path(match.group(1)).name
    return None


def init_db(db_path: str | Path = DEFAULT_DB_PATH) -> Path:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS cronicle_jobs (
              job_id TEXT PRIMARY KEY,
              event_id TEXT,
              category_id TEXT,
              plugin TEXT,
              hostname TEXT,
              time_start REAL NOT NULL,
              elapsed REAL,
              time_end REAL NOT NULL,
              status_code INTEGER,
              action TEXT,
              event_title TEXT,
              category_title TEXT,
              plugin_title TEXT,
              python_script TEXT,
              cpu_min REAL,
              cpu_max REAL,
              cpu_total REAL,
              cpu_count REAL,
              cpu_current REAL,
              cpu_avg REAL,
              mem_min REAL,
              mem_max REAL,
              mem_total REAL,
              mem_count REAL,
              mem_current REAL,
              mem_avg REAL,
              log_file_size INTEGER,
              collected_at REAL NOT NULL
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_cronicle_jobs_time ON cronicle_jobs(time_start, time_end)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_cronicle_jobs_cpu ON cronicle_jobs(cpu_avg, cpu_max)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_cronicle_jobs_mem ON cronicle_jobs(mem_avg, mem_max)")
    return db_path


def normalize_job(job: dict[str, Any], collected_at: float | None = None) -> dict[str, Any] | None:
    job_id = str(job.get("id") or "").strip()
    time_start = safe_float(job.get("time_start"))
    elapsed = safe_float(job.get("elapsed"))
    if not job_id or time_start is None:
        return None
    if elapsed is None:
        elapsed = 0.0
    time_end = safe_float(job.get("time_end"))
    if time_end is None:
        time_end = time_start + elapsed
    cpu = job.get("cpu") if isinstance(job.get("cpu"), dict) else {}
    mem = job.get("mem") if isinstance(job.get("mem"), dict) else {}
    cpu_count = safe_float(cpu.get("count"))
    mem_count = safe_float(mem.get("count"))
    cpu_total = safe_float(cpu.get("total"))
    mem_total = safe_float(mem.get("total"))
    return {
        "job_id": job_id,
        "event_id": str(job.get("event") or "") or None,
        "category_id": str(job.get("category") or "") or None,
        "plugin": str(job.get("plugin") or "") or None,
        "hostname": str(job.get("hostname") or "") or None,
        "time_start": time_start,
        "elapsed": elapsed,
        "time_end": time_end,
        "status_code": safe_int(job.get("code")),
        "action": str(job.get("action") or "") or None,
        "event_title": str(job.get("event_title") or job.get("title") or job.get("event") or "Unknown"),
        "category_title": str(job.get("category_title") or "") or None,
        "plugin_title": str(job.get("plugin_title") or "") or None,
        "python_script": extract_python_script(job),
        "cpu_min": safe_float(cpu.get("min")),
        "cpu_max": safe_float(cpu.get("max")),
        "cpu_total": cpu_total,
        "cpu_count": cpu_count,
        "cpu_current": safe_float(cpu.get("current")),
        "cpu_avg": (cpu_total / cpu_count) if cpu_total is not None and cpu_count and cpu_count > 0 else None,
        "mem_min": safe_float(mem.get("min")),
        "mem_max": safe_float(mem.get("max")),
        "mem_total": mem_total,
        "mem_count": mem_count,
        "mem_current": safe_float(mem.get("current")),
        "mem_avg": (mem_total / mem_count) if mem_total is not None and mem_count and mem_count > 0 else None,
        "log_file_size": safe_int(job.get("log_file_size")),
        "collected_at": collected_at if collected_at is not None else time.time(),
    }


def upsert_jobs(rows: list[dict[str, Any]], db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, int]:
    db_path = init_db(db_path)
    normalized = [r for row in rows if isinstance(row, dict) for r in [normalize_job(row)] if r]
    if not normalized:
        return {"seen": len(rows), "normalized": 0, "inserted_or_updated": 0}
    columns = list(normalized[0].keys())
    placeholders = ",".join([":" + c for c in columns])
    update_cols = [c for c in columns if c != "job_id"]
    update = ",".join([f"{c}=excluded.{c}" for c in update_cols])
    sql = f"INSERT INTO cronicle_jobs ({','.join(columns)}) VALUES ({placeholders}) ON CONFLICT(job_id) DO UPDATE SET {update}"
    with sqlite3.connect(db_path) as con:
        con.executemany(sql, normalized)
    return {"seen": len(rows), "normalized": len(normalized), "inserted_or_updated": len(normalized)}


def _credential_value(creds: dict[str, Any], name: str) -> str | None:
    value = creds.get(name)
    if isinstance(value, str) and value.strip():
        return value.strip()
    for v in creds.values():
        if isinstance(v, dict):
            found = _credential_value(v, name)
            if found:
                return found
    return None


def fetch_history_page(cronicle_url: str, cronicle_key: str, offset: int, limit: int, timeout: int = 30, verify_tls: bool = True) -> dict[str, Any]:
    base = cronicle_url.rstrip("/") + "/"
    endpoint = urljoin(base, "api/app/get_history/v1")
    url = endpoint + "?" + urlencode({"offset": offset, "limit": limit})
    req = Request(url, headers={"x-api-key": cronicle_key, "Accept": "application/json"}, method="GET")
    context = None if verify_tls else ssl._create_unverified_context()
    try:
        with urlopen(req, timeout=timeout, context=context) as resp:
            body = resp.read(20_000_000)
            return json.loads(body.decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"Cronicle history request failed with HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError("Cronicle history request failed") from exc


def collect_history(creds: dict[str, Any], db_path: str | Path = DEFAULT_DB_PATH, *, limit: int = 100, max_pages: int = 50, retention_days: int | None = 120, stop_before: str | datetime | None = None, verify_tls: bool = True) -> dict[str, Any]:
    cronicle_url = _credential_value(creds, "CRONICLE_URL")
    cronicle_key = _credential_value(creds, "CRONICLE_KEY")
    if not cronicle_url or not cronicle_key:
        raise RuntimeError("CRONICLE_URL or CRONICLE_KEY is missing from decrypted credentials")
    init_db(db_path)
    stop_before_epoch = epoch_seconds(parse_ts(stop_before)) if stop_before else None
    totals = {"pages": 0, "rows_seen": 0, "rows_normalized": 0, "rows_upserted": 0}
    for page in range(max_pages):
        offset = page * limit
        payload = fetch_history_page(cronicle_url, cronicle_key, offset, limit, verify_tls=verify_tls)
        rows = payload.get("rows") or payload.get("data") or []
        if not isinstance(rows, list):
            raise RuntimeError("Cronicle get_history response did not contain a rows list")
        stats = upsert_jobs(rows, db_path)
        totals["pages"] += 1
        totals["rows_seen"] += stats["seen"]
        totals["rows_normalized"] += stats["normalized"]
        totals["rows_upserted"] += stats["inserted_or_updated"]
        if stop_before_epoch is not None:
            starts = [safe_float(r.get("time_start")) for r in rows if isinstance(r, dict)]
            starts = [s for s in starts if s is not None]
            if starts and min(starts) < stop_before_epoch:
                totals["stopped_before_epoch"] = stop_before_epoch
                break
        if len(rows) < limit:
            break
    if retention_days is not None and retention_days > 0:
        cutoff = time.time() - retention_days * 86400
        with sqlite3.connect(db_path) as con:
            totals["retention_deleted"] = con.execute("DELETE FROM cronicle_jobs WHERE time_end < ?", (cutoff,)).rowcount
    totals["db_path"] = str(db_path)
    totals["tls_verification"] = "enabled" if verify_tls else "disabled_for_self_signed_endpoint"
    return totals


def analyze_jobs_near_anomaly(
    anomaly_time: str | datetime,
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    cpu_threshold: float | None = DEFAULT_CPU_THRESHOLD,
    memory_threshold: float | None = None,
    max_results: int = DEFAULT_MAX_RESULTS,
) -> dict[str, Any]:
    db_path = Path(db_path)
    ts = parse_ts(anomaly_time)
    if not ts:
        raise ValueError(f"Invalid anomaly_time: {anomaly_time}")
    window_start = ts - timedelta(minutes=window_minutes)
    window_end = ts + timedelta(minutes=window_minutes)
    if not db_path.exists():
        return {
            "status": "CronicleHistoryUnavailable",
            "timestamp": iso_z(ts),
            "window_minutes": window_minutes,
            "cpu_threshold": cpu_threshold,
            "memory_threshold": memory_threshold,
            "jobs": [],
            "note": "Cronicle SQLite history database is not available; run the collector before expecting job context.",
        }
    with sqlite3.connect(db_path) as con:
        coverage = con.execute("SELECT min(time_start), max(time_end), count(*) FROM cronicle_jobs").fetchone()
    if not coverage or not coverage[2] or coverage[0] is None or coverage[1] is None:
        return {
            "status": "CronicleHistoryUnavailable",
            "timestamp": iso_z(ts),
            "window_minutes": window_minutes,
            "cpu_threshold": cpu_threshold,
            "memory_threshold": memory_threshold,
            "jobs": [],
            "note": "Cronicle SQLite history database contains no collected job rows.",
        }
    if epoch_seconds(window_end) < float(coverage[0]) or epoch_seconds(window_start) > float(coverage[1]):
        return {
            "status": "CronicleHistoryOutOfRange",
            "timestamp": iso_z(ts),
            "window_minutes": window_minutes,
            "cpu_threshold": cpu_threshold,
            "memory_threshold": memory_threshold,
            "jobs": [],
            "note": "The selected hour is outside the currently collected Cronicle SQLite history coverage. No Cronicle result is shown rather than fabricating historical jobs.",
        }
    where = ["time_start <= ?", "time_end >= ?"]
    params: list[Any] = [epoch_seconds(window_end), epoch_seconds(window_start)]
    threshold_parts = []
    if cpu_threshold is not None:
        threshold_parts.extend(["cpu_avg >= ?", "cpu_max >= ?"])
        params.extend([cpu_threshold, cpu_threshold])
    if memory_threshold is not None:
        threshold_parts.extend(["mem_avg >= ?", "mem_max >= ?"])
        params.extend([memory_threshold, memory_threshold])
    if threshold_parts:
        where.append("(" + " OR ".join(threshold_parts) + ")")
    sql = """
        SELECT event_title, python_script, cpu_avg, cpu_max, mem_avg, mem_max, time_start, time_end
        FROM cronicle_jobs
        WHERE {where}
    """.format(where=" AND ".join(where))
    with sqlite3.connect(db_path) as con:
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute(sql, params)]
    def score(row: dict[str, Any]) -> tuple:
        cpu_values = [v for v in [row.get("cpu_avg"), row.get("cpu_max")] if v is not None]
        mem_values = [v for v in [row.get("mem_avg"), row.get("mem_max")] if v is not None]
        cpu_score = max(cpu_values) if cpu_values else -1.0
        mem_score = max(mem_values) if mem_values else -1.0
        return (cpu_score, mem_score, row.get("time_start") or 0, row.get("event_title") or "")
    rows.sort(key=score, reverse=True)
    minimal = []
    seen = set()
    for row in rows:
        scheduled_job = row.get("event_title") or "Unknown"
        python_script = row.get("python_script")
        key = (scheduled_job, python_script)
        if key in seen:
            continue
        seen.add(key)
        minimal.append({"scheduled_job": scheduled_job, "python_script": python_script})
        if len(minimal) >= max_results:
            break
    return {
        "status": "Available" if minimal else "NoJobsAboveThreshold",
        "timestamp": iso_z(ts),
        "window_minutes": window_minutes,
        "cpu_threshold": cpu_threshold,
        "memory_threshold": memory_threshold,
        "jobs": minimal,
        "note": "Cronicle jobs executing around the selected hour and meeting configured resource criteria. Supporting evidence only; no causality is inferred." if minimal else "No Cronicle jobs in the overlap window met the configured CPU/memory criteria.",
    }


def is_platform_mongo_row(row: dict[str, Any]) -> bool:
    return row.get("ResourceID") in PLATFORM_MONGO_COST_IDS or row.get("MongoDBResourceID") in PLATFORM_MONGO_HEALTH_IDS


def _context_for_point(context_by_key: dict[str, dict[str, Any]], resource_id: str, timestamp: str) -> dict[str, Any] | None:
    return context_by_key.get(f"{resource_id}|{timestamp}")


def enrich_mongo_health_rows(
    mongo_rows: list[dict[str, Any]],
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    cpu_threshold: float | None = DEFAULT_CPU_THRESHOLD,
    memory_threshold: float | None = None,
    max_results: int = DEFAULT_MAX_RESULTS,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    # Dashboard Cronicle KPI data must be derived from cronicle_history.sqlite3.
    # Strip any previously embedded Cronicle context before rebuilding from the
    # Analyzer so stale JSON artifacts cannot become the source of truth.
    for row in mongo_rows:
        for point in row.get("HourlyHealthData") or []:
            if isinstance(point, dict):
                point.pop("CronicleContext", None)
                point.pop("CronicleJobs", None)
        for points in (row.get("Metrics") or {}).values():
            if isinstance(points, list):
                for point in points:
                    if isinstance(point, dict):
                        point.pop("CronicleContext", None)
                        point.pop("CronicleJobs", None)
    context_by_key: dict[str, dict[str, Any]] = {}
    platform_rows = [r for r in mongo_rows if isinstance(r, dict) and is_platform_mongo_row(r)]
    for row in platform_rows:
        rid = row.get("ResourceID")
        timestamps = sorted({p.get("Timestamp") for p in row.get("HourlyHealthData") or [] if isinstance(p, dict) and p.get("Timestamp")})
        for ts in timestamps:
            analysis = analyze_jobs_near_anomaly(
                ts,
                db_path=db_path,
                window_minutes=window_minutes,
                cpu_threshold=cpu_threshold,
                memory_threshold=memory_threshold,
                max_results=max_results,
            )
            context_by_key[f"{rid}|{ts}"] = analysis
        row["CronicleCorrelationScope"] = "Cluster_Platform only, per Ops-Agent spec"
    for row in mongo_rows:
        rid = row.get("ResourceID")
        if not is_platform_mongo_row(row):
            row["CronicleCorrelationScope"] = "Not applicable: Ops-Agent Cronicle server is scoped only to Cluster_Platform"
            continue
        for point in row.get("HourlyHealthData") or []:
            if not isinstance(point, dict):
                continue
            ctx = _context_for_point(context_by_key, rid, point.get("Timestamp"))
            if ctx:
                point["CronicleContext"] = ctx
                point["CronicleJobs"] = ctx.get("jobs", [])
        for points in (row.get("Metrics") or {}).values():
            if not isinstance(points, list):
                continue
            for point in points:
                if not isinstance(point, dict):
                    continue
                ctx = _context_for_point(context_by_key, rid, point.get("Timestamp"))
                if ctx:
                    point["CronicleContext"] = ctx
                    point["CronicleJobs"] = ctx.get("jobs", [])
    summary = {
        "applies_to": "Cluster_Platform",
        "platform_resource_count": len(platform_rows),
        "point_count": len(context_by_key),
        "points_with_jobs": sum(1 for ctx in context_by_key.values() if ctx.get("jobs")),
        "window_minutes": window_minutes,
        "cpu_threshold": cpu_threshold,
        "memory_threshold": memory_threshold,
        "max_results": max_results,
        "status_counts": {},
        "context_by_point": context_by_key,
    }
    counts: dict[str, int] = {}
    for ctx in context_by_key.values():
        counts[ctx.get("status", "Unknown")] = counts.get(ctx.get("status", "Unknown"), 0) + 1
    summary["status_counts"] = counts
    return mongo_rows, summary


def build_run_cronicle_context(
    data_dir: str | Path,
    run_id: str,
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    cpu_threshold: float | None = DEFAULT_CPU_THRESHOLD,
    memory_threshold: float | None = None,
    max_results: int = DEFAULT_MAX_RESULTS,
) -> dict[str, Any]:
    data_dir = Path(data_dir)
    sys.path.insert(0, str(data_dir / "cost-health-dashboard"))
    import dashboard_api  # local import to avoid mandatory dependency for tests

    run = dashboard_api.get_run(data_dir, run_id)
    mongo_path = run["files"].get("mongo_health")
    if not mongo_path or not Path(mongo_path).exists():
        raise RuntimeError(f"Mongo health file is not available for run {run_id}")
    with Path(mongo_path).open(encoding="utf-8") as fh:
        mongo_rows = json.load(fh)
    enriched, context = enrich_mongo_health_rows(
        mongo_rows,
        db_path=db_path,
        window_minutes=window_minutes,
        cpu_threshold=cpu_threshold,
        memory_threshold=memory_threshold,
        max_results=max_results,
    )
    Path(mongo_path).write_text(json.dumps(enriched, ensure_ascii=False, indent=2), encoding="utf-8")
    context_file = data_dir / f"Cronicle_Analysis_{run_id}.json"
    public_context = {k: v for k, v in context.items() if k != "context_by_point"}
    # Persist dashboard-facing point data only; no hostnames, job IDs, or CPU/RAM metrics.
    public_context["points"] = context["context_by_point"]
    context_file.write_text(json.dumps(public_context, ensure_ascii=False, indent=2), encoding="utf-8")
    summary_path = run["files"].get("summary")
    if summary_path and Path(summary_path).exists():
        summary = json.loads(Path(summary_path).read_text(encoding="utf-8"))
        summary.update({
            "cronicle_analysis_file": str(context_file),
            "cronicle_correlation_scope": "Cluster_Platform only",
            "cronicle_context_point_count": context["point_count"],
            "cronicle_context_points_with_jobs": context["points_with_jobs"],
            "cronicle_context_status_counts": context["status_counts"],
            "cronicle_window_minutes": window_minutes,
            "cronicle_cpu_threshold": cpu_threshold,
            "cronicle_memory_threshold": memory_threshold,
            "cronicle_max_results": max_results,
        })
        Path(summary_path).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "run_id": run_id, "mongo_health_file": str(mongo_path), "cronicle_analysis_file": str(context_file), **public_context}


def decrypt_credentials_from_password(password: str) -> dict[str, Any]:
    sys.path.insert(0, str(DEFAULT_DATA_DIR))
    import cost_health_analysis_agent as cost_agent
    return cost_agent.decrypt_credentials(password)


def main() -> int:
    parser = argparse.ArgumentParser(description="Cronicle collector/analyzer for CloudVitals")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_collect = sub.add_parser("collect")
    p_collect.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    p_collect.add_argument("--limit", type=int, default=100)
    p_collect.add_argument("--max-pages", type=int, default=50)
    p_collect.add_argument("--retention-days", type=int, default=120)
    p_collect.add_argument("--stop-before", default=None, help="Stop paginating once a page contains rows older than this ISO UTC timestamp")
    p_collect.add_argument("--insecure-tls", action="store_true", help="Disable TLS certificate verification for known self-signed Cronicle endpoints")
    p_collect.add_argument("--password-stdin", action="store_true")
    p_build = sub.add_parser("build-run")
    p_build.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    p_build.add_argument("--run-id", required=True)
    p_build.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    p_build.add_argument("--window-minutes", type=int, default=DEFAULT_WINDOW_MINUTES)
    p_build.add_argument("--cpu-threshold", type=float, default=DEFAULT_CPU_THRESHOLD)
    p_build.add_argument("--memory-threshold", type=float, default=None)
    p_build.add_argument("--max-results", type=int, default=DEFAULT_MAX_RESULTS)
    p_analyze = sub.add_parser("analyze")
    p_analyze.add_argument("--timestamp", required=True)
    p_analyze.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    p_analyze.add_argument("--window-minutes", type=int, default=DEFAULT_WINDOW_MINUTES)
    p_analyze.add_argument("--cpu-threshold", type=float, default=DEFAULT_CPU_THRESHOLD)
    p_analyze.add_argument("--memory-threshold", type=float, default=None)
    p_analyze.add_argument("--max-results", type=int, default=DEFAULT_MAX_RESULTS)
    args = parser.parse_args()

    if args.cmd == "collect":
        password = sys.stdin.readline().rstrip("\n") if args.password_stdin else getpass.getpass("Credential password: ")
        try:
            creds = decrypt_credentials_from_password(password)
            result = collect_history(creds, args.db_path, limit=args.limit, max_pages=args.max_pages, retention_days=args.retention_days, stop_before=args.stop_before, verify_tls=not args.insecure_tls)
        finally:
            password = None
        print(json.dumps(result, indent=2))
        return 0
    if args.cmd == "build-run":
        result = build_run_cronicle_context(
            args.data_dir,
            args.run_id,
            db_path=args.db_path,
            window_minutes=args.window_minutes,
            cpu_threshold=args.cpu_threshold,
            memory_threshold=args.memory_threshold,
            max_results=args.max_results,
        )
        print(json.dumps(result, indent=2))
        return 0
    if args.cmd == "analyze":
        result = analyze_jobs_near_anomaly(
            args.timestamp,
            db_path=args.db_path,
            window_minutes=args.window_minutes,
            cpu_threshold=args.cpu_threshold,
            memory_threshold=args.memory_threshold,
            max_results=args.max_results,
        )
        print(json.dumps(result, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
