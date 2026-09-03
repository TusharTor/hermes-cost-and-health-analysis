#!/usr/bin/env python3
"""Cost + Health dashboard backend.

Zero-dependency stdlib HTTP API that reads the analysis artifacts produced in
/opt/data by the Cost and Health Analysis Agent.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

DEFAULT_DATA_DIR = Path(os.environ.get("COST_HEALTH_DATA_DIR", "/opt/data"))
DEFAULT_CRONICLE_DB_PATH = Path(os.environ.get("CRONICLE_HISTORY_DB", "/opt/data/cronicle_history.sqlite3"))
DEFAULT_CRONICLE_WINDOW_MINUTES = int(os.environ.get("CRONICLE_WINDOW_MINUTES", "30"))
DEFAULT_CRONICLE_CPU_THRESHOLD = float(os.environ.get("CRONICLE_CPU_THRESHOLD", "50"))
DEFAULT_CRONICLE_MAX_RESULTS = int(os.environ.get("CRONICLE_MAX_RESULTS", "10"))
APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
ROUTE_PREFIX = "/tor-ops-agent/dashboard"
SEVERITY_RANK = {"Normal": 0, "Low": 1, "Medium": 2, "High": 3, "Critical": 4}
RUN_ID_RE = re.compile(r"_(\d{6}_\d{6})\.")


class DashboardError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def run_id_from_path(path: str | Path) -> str | None:
    match = RUN_ID_RE.search(str(path))
    return match.group(1) if match else None


def read_json(path: str | Path) -> Any:
    with Path(path).open(encoding="utf-8") as fh:
        return json.load(fh)


def _file_or_none(path: str | Path | None, data_dir: Path) -> str | None:
    if not path:
        return None
    candidate = Path(str(path))
    if not candidate.is_absolute():
        candidate = data_dir / candidate
    return str(candidate) if candidate.exists() else None


def mongo_run_id_from_run_id(run_id: str) -> str:
    """Convert DDMMYY_HHMMSS to the Mongo filename's DDMMYYYY_HHMMSS."""
    try:
        date_part, time_part = run_id.split("_", 1)
        if len(date_part) == 6:
            return f"{date_part[:4]}20{date_part[4:]}_{time_part}"
    except ValueError:
        pass
    return run_id


def _summary_to_run(summary_path: Path, data_dir: Path) -> dict[str, Any]:
    summary = read_json(summary_path)
    run_id = run_id_from_path(summary_path) or run_id_from_path(summary.get("cost_file", ""))
    if not run_id:
        raise DashboardError(500, f"Cannot derive run_id from {summary_path}")
    health_ts = _file_or_none(summary.get("health_timeseries_file"), data_dir)
    if not health_ts:
        inferred = data_dir / f"Health-Timeseries_{run_id}.json"
        health_ts = str(inferred) if inferred.exists() else None
    azure_health = _file_or_none(summary.get("azure_health_analysis_file"), data_dir)
    if not azure_health:
        inferred = data_dir / f"Azure_Health_Analysis_{run_id}.json"
        azure_health = str(inferred) if inferred.exists() else None
    mongo_health = _file_or_none(summary.get("mongo_health_analysis_file"), data_dir)
    if not mongo_health:
        inferred = data_dir / f"Mongo_Health_Analysis_{mongo_run_id_from_run_id(run_id)}.json"
        mongo_health = str(inferred) if inferred.exists() else None
    cronicle_analysis = _file_or_none(summary.get("cronicle_analysis_file"), data_dir)
    if not cronicle_analysis:
        inferred = data_dir / f"Cronicle_Analysis_{run_id}.json"
        cronicle_analysis = str(inferred) if inferred.exists() else None
    files = {
        "summary": str(summary_path),
        "cost": _file_or_none(summary.get("cost_file"), data_dir) or str(data_dir / f"Cost-Analysis_{run_id}.json"),
        "cost_meta": _file_or_none(summary.get("cost_meta_file"), data_dir) or str(data_dir / f"Cost-Analysis_{run_id}.meta.json"),
        "health": _file_or_none(summary.get("health_file"), data_dir) or str(data_dir / f"Health-Analysis_{run_id}.json"),
        "azure_health": azure_health,
        "mongo_health": mongo_health,
        "health_timeseries": health_ts,
        "cronicle_analysis": cronicle_analysis,
        "chart": _file_or_none(summary.get("chart_file"), data_dir),
    }
    return {
        "run_id": run_id,
        "fromDate": summary.get("fromDate"),
        "toDate": summary.get("toDate"),
        "summary": summary,
        "files": files,
        "has_health_timeseries": bool(health_ts),
        "has_split_health_analysis": bool(azure_health or mongo_health),
    }


def _infer_runs_without_summary(data_dir: Path) -> list[dict[str, Any]]:
    runs = []
    for cost_path in data_dir.glob("Cost-Analysis_*.json"):
        if cost_path.name.endswith(".meta.json"):
            continue
        run_id = run_id_from_path(cost_path)
        if not run_id:
            continue
        health_path = data_dir / f"Health-Analysis_{run_id}.json"
        meta_path = data_dir / f"Cost-Analysis_{run_id}.meta.json"
        health_ts_path = data_dir / f"Health-Timeseries_{run_id}.json"
        if not health_path.exists():
            continue
        meta = read_json(meta_path) if meta_path.exists() else {}
        summary = {
            "fromDate": meta.get("fromDate"),
            "toDate": meta.get("toDate"),
            "cost_file": str(cost_path),
            "cost_meta_file": str(meta_path) if meta_path.exists() else None,
            "health_file": str(health_path),
        }
        azure_path = data_dir / f"Azure_Health_Analysis_{run_id}.json"
        mongo_path = data_dir / f"Mongo_Health_Analysis_{mongo_run_id_from_run_id(run_id)}.json"
        runs.append({
            "run_id": run_id,
            "fromDate": summary.get("fromDate"),
            "toDate": summary.get("toDate"),
            "summary": summary,
            "files": {
                "summary": None,
                "cost": str(cost_path),
                "cost_meta": str(meta_path) if meta_path.exists() else None,
                "health": str(health_path),
                "azure_health": str(azure_path) if azure_path.exists() else None,
                "mongo_health": str(mongo_path) if mongo_path.exists() else None,
                "health_timeseries": str(health_ts_path) if health_ts_path.exists() else None,
                "cronicle_analysis": str(data_dir / f"Cronicle_Analysis_{run_id}.json") if (data_dir / f"Cronicle_Analysis_{run_id}.json").exists() else None,
                "chart": None,
            },
            "has_health_timeseries": health_ts_path.exists(),
            "has_split_health_analysis": azure_path.exists() or mongo_path.exists(),
        })
    return runs


def discover_runs(data_dir: str | Path = DEFAULT_DATA_DIR) -> list[dict[str, Any]]:
    """Discover cost-health analysis runs in newest-first order."""
    data_dir = Path(data_dir)
    runs: dict[str, dict[str, Any]] = {}
    for summary_path in data_dir.glob("Cost-Health-Summary_*.json"):
        try:
            run = _summary_to_run(summary_path, data_dir)
            runs[run["run_id"]] = run
        except Exception:
            continue
    for run in _infer_runs_without_summary(data_dir):
        runs.setdefault(run["run_id"], run)
    ordered = sorted(runs.values(), key=lambda r: r["run_id"], reverse=True)
    # Keep payload light for /api/runs; detailed summary is available separately.
    return ordered


def get_run(data_dir: str | Path, run_id: str | None = None) -> dict[str, Any]:
    runs = discover_runs(data_dir)
    if not runs:
        raise DashboardError(404, "No Cost/Health analysis runs found in the data directory.")
    if not run_id or run_id == "latest":
        return runs[0]
    for run in runs:
        if run["run_id"] == run_id:
            return run
    raise DashboardError(404, f"Run not found: {run_id}")


def _load_run_file(data_dir: str | Path, run_id: str | None, kind: str) -> Any:
    run = get_run(data_dir, run_id)
    path = run["files"].get(kind)
    if not path or not Path(path).exists():
        raise DashboardError(404, f"{kind} file is not available for run {run['run_id']}")
    return read_json(path)


def _extract_cost_rows(payload: Any) -> list[dict[str, Any]]:
    """Return historical cost-analysis rows from legacy list or forecast-aware dict payloads."""
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in [
            "CostAnalysis",
            "CostAnalysisRecords",
            "CostRecords",
            "Records",
            "Data",
            "Items",
            "Costs",
            "cost_analysis",
            "cost_records",
        ]:
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
        if payload.get("ResourceID"):
            return [payload]
    return []


def _extract_forecast(payload: Any, run: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return forecast payload from Cost-Analysis or summary without assuming one exact shape."""
    if isinstance(payload, dict):
        forecast = payload.get("Forecast") or payload.get("forecast")
        if isinstance(forecast, dict):
            return forecast
    if run:
        forecast = (run.get("summary") or {}).get("Forecast") or (run.get("summary") or {}).get("forecast")
        if isinstance(forecast, dict):
            return forecast
    return {}


def _is_predicted_cost_row(row: dict[str, Any]) -> bool:
    marker = str(row.get("PointType") or row.get("CostType") or row.get("Type") or "").lower()
    return bool(row.get("IsPredicted") or row.get("Predicted") or marker in {"predicted", "forecast", "forecasted"})


def _prediction_date_value(point: dict[str, Any]) -> tuple[str | None, float | None]:
    date = point.get("Date") or point.get("AnalysisDate") or point.get("ForecastDate") or point.get("UsageDate")
    value = _safe_float(point.get("PredictedCost"))
    if value is None:
        value = _safe_float(point.get("CostAmount"))
    if value is None:
        value = _safe_float(point.get("Value"))
    return (str(date)[:10] if date else None, value)


def _forecast_meta(forecast: dict[str, Any], resource_forecast: dict[str, Any] | None = None) -> dict[str, Any]:
    resource_forecast = resource_forecast or {}
    return {
        "ForecastStart": resource_forecast.get("ForecastStart") or forecast.get("ForecastStart"),
        "ForecastEnd": resource_forecast.get("ForecastEnd") or forecast.get("ForecastEnd"),
        "ForecastDays": resource_forecast.get("ForecastDays") or forecast.get("ForecastDays"),
        "ForecastModel": resource_forecast.get("ForecastModel") or forecast.get("ForecastModel"),
        "ValidationMetrics": resource_forecast.get("ValidationMetrics") or forecast.get("ValidationMetrics"),
        "ValidationStatus": resource_forecast.get("ValidationStatus") or forecast.get("ValidationStatus"),
    }


def _normalise_prediction_point(point: dict[str, Any], forecast: dict[str, Any], resource_forecast: dict[str, Any] | None = None, resource_id: str | None = None) -> dict[str, Any] | None:
    date, value = _prediction_date_value(point)
    if not date or value is None:
        return None
    meta = _forecast_meta(forecast, resource_forecast)
    row = {
        "AnalysisDate": date,
        "CostAmount": value,
        "PredictedCost": value,
        "IsPredicted": True,
        "PointType": "Predicted",
        "TrendStatus": "Predicted Cost",
        "Severity": "Normal",
        "AnalysisReason": "Forecasted cost point generated from the selected-period training data.",
    }
    if resource_id:
        row["ResourceID"] = resource_id
    for key, val in meta.items():
        if val is not None:
            row[key] = val
    return row


def _daily_predictions(container: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(container, dict):
        return []
    for key in ["DailyPredictions", "daily_predictions", "Predictions", "predictions"]:
        value = container.get(key)
        if isinstance(value, list):
            return [point for point in value if isinstance(point, dict)]
    return []


def _overall_forecast_points(payload: Any, run: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    forecast = _extract_forecast(payload, run)
    overall = forecast.get("Overall") or forecast.get("overall") or {}
    return [row for point in _daily_predictions(overall) if (row := _normalise_prediction_point(point, forecast, overall))]


def _resource_forecast_points(payload: Any, resource_id: str, run: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    forecast = _extract_forecast(payload, run)
    containers = []
    for key in ["AffectedResources", "ResourceForecasts", "Resources", "resource_forecasts"]:
        value = forecast.get(key)
        if isinstance(value, list):
            containers.extend(item for item in value if isinstance(item, dict) and item.get("ResourceID") == resource_id)
        elif isinstance(value, dict):
            item = value.get(resource_id)
            if isinstance(item, dict):
                containers.append({"ResourceID": resource_id, **item})
    predicted: list[dict[str, Any]] = []
    for container in containers:
        for point in _daily_predictions(container):
            row = _normalise_prediction_point(point, forecast, container, resource_id)
            if row:
                predicted.append(row)
    return predicted


def _merge_actual_and_predicted(actual: list[dict[str, Any]], predicted: list[dict[str, Any]]) -> list[dict[str, Any]]:
    actual_dates = {str(row.get("AnalysisDate")) for row in actual if row.get("AnalysisDate")}
    merged = list(actual)
    for row in predicted:
        if str(row.get("AnalysisDate")) in actual_dates:
            continue
        merged.append(row)
    return sorted(merged, key=lambda r: (str(r.get("AnalysisDate") or ""), 1 if _is_predicted_cost_row(r) else 0))


def health_summary_map(data_dir: str | Path, run_id: str | None) -> dict[str, dict[str, Any]]:
    try:
        rows = _load_run_file(data_dir, run_id, "health")
    except DashboardError:
        return {}
    return {r.get("ResourceID"): r for r in rows if r.get("ResourceID")}


def affected_resources(data_dir: str | Path = DEFAULT_DATA_DIR, run_id: str | None = None) -> list[dict[str, Any]]:
    cost_rows = _extract_cost_rows(_load_run_file(data_dir, run_id, "cost"))
    hmap = health_summary_map(data_dir, run_id)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in cost_rows:
        rid = row.get("ResourceID")
        if rid:
            grouped[rid].append(row)
    resources = []
    for rid, rows in grouped.items():
        peak = max(rows, key=lambda r: float(r.get("CostAmount") or 0))
        severities = [r.get("Severity", "Normal") for r in rows]
        max_sev = max(severities, key=lambda s: SEVERITY_RANK.get(s, 0))
        anomalies = [r for r in rows if r.get("IsAnomaly")]
        h = hmap.get(rid, {})
        resources.append({
            "ResourceID": rid,
            "ResourceName": rid.rstrip("/").split("/")[-1] if "/" in rid else rid,
            "ResourceType": rows[0].get("ResourceType", "Unknown"),
            "Trend": rows[0].get("Trend", "Unknown"),
            "MaxSeverity": max_sev,
            "AnomalyCount": len(anomalies),
            "PointCount": len(rows),
            "PeakCost": peak.get("CostAmount"),
            "PeakCostDate": peak.get("AnalysisDate"),
            "CostHealthCorrelation": h.get("CostHealthCorrelation", "Not Available"),
            "OverallHealthStatus": h.get("OverallHealthStatus", "Not Available"),
            "CPUStatus": h.get("CPUStatus", "Not Available"),
            "MemoryStatus": h.get("MemoryStatus", "Not Available"),
            "DiskStatus": h.get("DiskStatus", "Not Available"),
            "NetworkStatus": h.get("NetworkStatus", "Not Available"),
        })
    return sorted(resources, key=lambda r: (SEVERITY_RANK.get(r["MaxSeverity"], 0), r.get("PeakCost") or 0, r["AnomalyCount"]), reverse=True)


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("$numberDecimal") or value.get("$numberDouble") or value.get("$numberInt") or value.get("$numberLong")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _cronicle_context_for_mongo_point(row: dict[str, Any], timestamp: str) -> dict[str, Any] | None:
    """Return dashboard-safe Cronicle KPI context from SQLite only."""
    if row.get("ResourceID") != "Platform_MongoDb" and row.get("MongoDBResourceID") != "Cluster_Platform":
        return None
    if not timestamp:
        return None
    try:
        import cronicle_correlation
        return cronicle_correlation.analyze_jobs_near_anomaly(
            timestamp,
            db_path=DEFAULT_CRONICLE_DB_PATH,
            window_minutes=DEFAULT_CRONICLE_WINDOW_MINUTES,
            cpu_threshold=DEFAULT_CRONICLE_CPU_THRESHOLD,
            max_results=DEFAULT_CRONICLE_MAX_RESULTS,
        )
    except Exception as exc:
        return {
            "status": "CronicleAnalyzerUnavailable",
            "timestamp": timestamp,
            "window_minutes": DEFAULT_CRONICLE_WINDOW_MINUTES,
            "cpu_threshold": DEFAULT_CRONICLE_CPU_THRESHOLD,
            "jobs": [],
            "note": f"Cronicle Analyzer could not read cronicle_history.sqlite3: {type(exc).__name__}.",
        }


def cost_timeseries(data_dir: str | Path = DEFAULT_DATA_DIR, run_id: str | None = None, resource_id: str | None = None) -> list[dict[str, Any]]:
    if not resource_id:
        raise DashboardError(400, "resource_id is required")
    run = get_run(data_dir, run_id)
    payload = _load_run_file(data_dir, run["run_id"], "cost")
    rows = _extract_cost_rows(payload)
    selected = [r for r in rows if r.get("ResourceID") == resource_id and not _is_predicted_cost_row(r)]
    predicted = _resource_forecast_points(payload, resource_id, run)
    if not selected and not predicted:
        raise DashboardError(404, f"No cost rows found for resource: {resource_id}")
    keys = ["ResourceID", "ResourceType", "AnalysisDate", "CostAmount", "AverageCost", "PreviousCost", "DayOverDayChange", "PercentageChange", "Trend", "TrendStatus", "IsAnomaly", "AnomalyType", "ExpectedCost", "Deviation", "DeviationPercentage", "ZScore", "Severity", "IsPeakCost", "IsMinimumCost", "AnalysisReason", "PredictedCost", "IsPredicted", "PointType", "ForecastStart", "ForecastEnd", "ForecastDays", "ForecastModel", "ValidationMetrics", "ValidationStatus"]
    compact = [{k: r.get(k) for k in keys if k in r} for r in selected]
    return _merge_actual_and_predicted(compact, predicted)


def overall_cost_timeseries(data_dir: str | Path = DEFAULT_DATA_DIR, run_id: str | None = None) -> list[dict[str, Any]]:
    """Aggregate actual daily cost and append overall forecast points when present."""
    run = get_run(data_dir, run_id)
    payload = _load_run_file(data_dir, run["run_id"], "cost")
    rows = _extract_cost_rows(payload)
    totals: dict[str, float] = defaultdict(float)
    for row in rows:
        if _is_predicted_cost_row(row):
            continue
        analysis_date = row.get("AnalysisDate")
        cost_amount = _safe_float(row.get("CostAmount"))
        if not analysis_date or cost_amount is None:
            continue
        totals[str(analysis_date)] += cost_amount
    actual = [{"AnalysisDate": day, "CostAmount": totals[day]} for day in sorted(totals)]
    predicted = _overall_forecast_points(payload, run)
    return _merge_actual_and_predicted(actual, predicted)


def _series_from_split_health_records(rows: list[dict[str, Any]], resource_id: str, date: str | None, source: str) -> list[dict[str, Any]]:
    """Flatten Azure_Health_Analysis/Mongo_Health_Analysis resource records into dashboard series."""
    output: list[dict[str, Any]] = []
    mongo_graph_categories = {"Connections", "MemoryUsage", "StorageSize"}
    for row in rows:
        if row.get("ResourceID") != resource_id:
            continue
        metrics = row.get("Metrics") or {}
        if not isinstance(metrics, dict):
            continue
        row_is_mongo = source == "Mongo_Health_Analysis" or any(marker in str(row.get("HealthSource") or "") for marker in ["MongoDB", "MongoAtlas"])
        for category, points in metrics.items():
            if row_is_mongo and str(category) not in mongo_graph_categories:
                continue
            if not isinstance(points, list):
                continue
            selected = []
            for point in points:
                if not isinstance(point, dict):
                    continue
                ts = str(point.get("Timestamp") or "")
                if date and not ts.startswith(date):
                    continue
                value = _safe_float(point.get("Value"))
                if value is None:
                    continue
                compact_point = {"Timestamp": ts, "Value": value}
                for extra_key in ["Tier", "SlowQueryCount", "SlowQueryNamespaces", "MemoryTotalMB", "CpuCores", "MemoryResidentMB", "StorageSizeMB"]:
                    if extra_key in point:
                        compact_point[extra_key] = point.get(extra_key)
                if row_is_mongo:
                    cronicle_context = _cronicle_context_for_mongo_point(row, ts)
                    if cronicle_context is not None:
                        compact_point["CronicleContext"] = cronicle_context
                        compact_point["CronicleJobs"] = cronicle_context.get("jobs", [])
                selected.append(compact_point)
            if not selected:
                continue
            selected.sort(key=lambda p: p.get("Timestamp") or "")
            first_source_point = next((p for p in points if isinstance(p, dict) and str(p.get("Timestamp") or "").startswith(date or "")), points[0] if points else {})
            output.append({
                "ResourceID": resource_id,
                "ResourceType": row.get("ResourceType"),
                "Date": date or (selected[0]["Timestamp"][:10] if selected and selected[0].get("Timestamp") else None),
                "MetricCategory": str(category),
                "MetricName": first_source_point.get("MetricName") or str(category),
                "Unit": first_source_point.get("Unit") or "Unknown",
                "Aggregation": first_source_point.get("Aggregation") or row.get("Aggregation") or "Hourly",
                "HealthSource": row.get("HealthSource") or source,
                "Points": selected,
            })
    preferred = ["CPU", "MemoryUsage", "Disk", "Network", "SNAT", "TrafficGiB", "AvgConn", "SNATPeak", "Connections", "StorageSize", "IOPs"]
    mongo_preferred = ["Connections", "MemoryUsage", "StorageSize"]
    rank = {name: i for i, name in enumerate(preferred)}
    mongo_rank = {name: i for i, name in enumerate(mongo_preferred)}
    if source == "Mongo_Health_Analysis":
        return sorted(output, key=lambda s: (mongo_rank.get(s.get("MetricCategory"), 999), s.get("MetricCategory") or ""))
    return sorted(output, key=lambda s: (rank.get(s.get("MetricCategory"), 999), s.get("MetricCategory") or ""))


def _split_no_data_message(source: str, rows: list[dict[str, Any]], date: str | None) -> str:
    coverages = {str(row.get("TemporalCoverage")) for row in rows if isinstance(row, dict) and row.get("TemporalCoverage")}
    coverage_notes = [str(row.get("CoverageNote")) for row in rows if isinstance(row, dict) and row.get("CoverageNote")]
    metric_errors = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        metric_errors.extend(row.get("MetricErrors") or [])
    if "AzureMetricRetentionExpired" in coverages or any("Max metrics retention period" in str(e.get("Error")) for e in metric_errors if isinstance(e, dict)):
        return f"{source} contains this resource, but {date or 'the selected date'} is outside Azure Monitor platform metrics retention. No hourly Azure metric graph can be drawn from Azure Monitor for that historical point."
    if "HourlySnapshotsUnavailable" in coverages:
        return coverage_notes[0] if coverage_notes else f"{source} contains this MongoDB resource, but no command-derived hourly snapshots matched {date or 'the selected date'}. Values were not fabricated or backfilled."
    if metric_errors:
        first_error = next((e for e in metric_errors if isinstance(e, dict) and e.get("Error")), None)
        if first_error:
            return f"{source} contains this resource, but no hourly points matched {date or 'the selected date'} because metric lookup failed: {first_error.get('Error')}"
    return f"{source} contains this resource, but no hourly points matched {date or 'the selected date'}. Showing summary context without fabricated graph points."


def _split_health_timeseries(run: dict[str, Any], resource_id: str, date: str | None) -> dict[str, Any] | None:
    """Return split Azure/Mongo health series when either new artifact contains the resource."""
    for kind, source, health_kind in [
        ("azure_health", "Azure_Health_Analysis", "azure"),
        ("mongo_health", "Mongo_Health_Analysis", "mongodb"),
    ]:
        path = run["files"].get(kind)
        if not path or not Path(path).exists():
            continue
        rows = read_json(path)
        if not isinstance(rows, list):
            continue
        matching_rows = [row for row in rows if isinstance(row, dict) and row.get("ResourceID") == resource_id]
        if not matching_rows:
            continue
        series = _series_from_split_health_records(rows, resource_id, date, source)
        message = None if series else _split_no_data_message(source, matching_rows, date)
        return {"source": source, "health_kind": health_kind, "series": series, "message": message}
    return None


def health_timeseries(data_dir: str | Path = DEFAULT_DATA_DIR, run_id: str | None = None, resource_id: str | None = None, date: str | None = None) -> dict[str, Any]:
    if not resource_id:
        raise DashboardError(400, "resource_id is required")
    run = get_run(data_dir, run_id)
    hmap = health_summary_map(data_dir, run["run_id"])
    summary = hmap.get(resource_id)
    split = _split_health_timeseries(run, resource_id, date)
    if split:
        return {"source": split["source"], "health_kind": split["health_kind"], "run_id": run["run_id"], "ResourceID": resource_id, "date": date, "series": split["series"], "summary": summary, "message": split.get("message")}
    ts_path = run["files"].get("health_timeseries")
    if ts_path and Path(ts_path).exists():
        all_series = read_json(ts_path)
        if not all_series:
            return {
                "source": "Health-Analysis summary",
                "run_id": run["run_id"],
                "ResourceID": resource_id,
                "date": date,
                "series": [],
                "summary": summary,
                "message": "Hourly health time-series generation produced 0 usable series for this run. Showing summary-only health status without fabricated graph points.",
            }
        series = []
        for item in all_series:
            if item.get("ResourceID") != resource_id:
                continue
            if date and item.get("Date") and item.get("Date") != date:
                continue
            if date and not item.get("Date"):
                points = item.get("Points") or []
                if not any(str(p.get("Timestamp", "")).startswith(date) for p in points):
                    continue
            series.append(item)
        return {"source": "Health-Timeseries", "run_id": run["run_id"], "ResourceID": resource_id, "date": date, "series": series, "summary": summary, "message": None if series else "Hourly file exists, but no series matched this resource/date."}
    return {
        "source": "Health-Analysis summary",
        "run_id": run["run_id"],
        "ResourceID": resource_id,
        "date": date,
        "series": [],
        "summary": summary,
        "message": "No hourly health time-series file is available for this run. Showing summary-only health status without fabricated graph points.",
    }


def run_summary(data_dir: str | Path = DEFAULT_DATA_DIR, run_id: str | None = None) -> dict[str, Any]:
    run = get_run(data_dir, run_id)
    return {"run_id": run["run_id"], **run["summary"], "files": run["files"], "has_health_timeseries": run["has_health_timeseries"], "has_split_health_analysis": run.get("has_split_health_analysis", False)}


def api_payload(path: str, query: dict[str, list[str]], data_dir: Path) -> Any:
    run_id = (query.get("run_id") or ["latest"])[0]
    if path == "/api/runs":
        return [{k: r[k] for k in ["run_id", "fromDate", "toDate", "files", "has_health_timeseries"]} | {"has_split_health_analysis": r.get("has_split_health_analysis", False), "summary_counts": {kk: r["summary"].get(kk) for kk in ["resource_count", "affected_resource_count", "cost_anomaly_records", "health_analysis_records"]}} for r in discover_runs(data_dir)]
    if path == "/api/summary":
        return run_summary(data_dir, run_id)
    if path == "/api/resources":
        return affected_resources(data_dir, run_id)
    if path == "/api/cost/overall":
        return overall_cost_timeseries(data_dir, run_id)
    if path == "/api/cost":
        return cost_timeseries(data_dir, run_id, (query.get("resource_id") or [None])[0])
    if path == "/api/health":
        return health_timeseries(data_dir, run_id, (query.get("resource_id") or [None])[0], (query.get("date") or [None])[0])
    raise DashboardError(404, f"Unknown API path: {path}")


def normalize_request_path(path: str) -> tuple[str, bool]:
    """Normalize standalone and /tor-ops-agent/dashboard-prefixed routes.

    Returns ``(normalized_path, used_prefix)``. The prefix lets CloudVitals run
    as its own project under a stable path without registering a Hermes UI tab.
    """
    if path in (ROUTE_PREFIX, f"{ROUTE_PREFIX}/"):
        return "/index.html", True
    if path.startswith(f"{ROUTE_PREFIX}/"):
        suffix = path[len(ROUTE_PREFIX):] or "/"
        return suffix, True
    if path in ("/", "/index.html"):
        return "/index.html", False
    return path, False


class DashboardHandler(BaseHTTPRequestHandler):
    data_dir = DEFAULT_DATA_DIR
    static_dir = STATIC_DIR

    def log_message(self, fmt: str, *args: Any) -> None:
        print("%s - - [%s] %s" % (self.client_address[0], self.log_date_time_string(), fmt % args))

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path, _used_prefix = normalize_request_path(unquote(parsed.path))
        query = parse_qs(parsed.query)
        try:
            if path.startswith("/api/"):
                return self._send_json(200, api_payload(path, query, self.data_dir))
            if path == "/index.html":
                return self._send_file(self.static_dir / "index.html")
            safe = path.lstrip("/")
            file_path = (self.static_dir / safe).resolve()
            if not str(file_path).startswith(str(self.static_dir.resolve())) or not file_path.exists() or not file_path.is_file():
                raise DashboardError(404, "Static file not found")
            return self._send_file(file_path)
        except DashboardError as e:
            self._send_json(e.status, {"ok": False, "error": e.message})
        except Exception as e:
            self._send_json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})


def serve(host: str = "127.0.0.1", port: int = 8765, data_dir: str | Path = DEFAULT_DATA_DIR) -> None:
    DashboardHandler.data_dir = Path(data_dir)
    httpd = ThreadingHTTPServer((host, port), DashboardHandler)
    print(f"Cost Health Dashboard running at http://{host}:{port}/")
    print(f"Cost Health Dashboard prefixed URL: http://{host}:{port}{ROUTE_PREFIX}")
    print(f"Reading analysis files from {DashboardHandler.data_dir}")
    httpd.serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve Cost + Health dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    args = parser.parse_args()
    serve(args.host, args.port, args.data_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
