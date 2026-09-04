import json
import sqlite3
from pathlib import Path

import cronicle_correlation as cc


def test_normalize_job_calculates_time_end_and_resource_avgs():
    job = {
        "id": "job1",
        "time_start": 1788415380.105,
        "elapsed": 817.037,
        "event_title": "ExampleJob",
        "command": "python /apps/workers/process_health.py --once",
        "cpu": {"min": 1, "max": 80, "total": 200, "count": 4, "current": 50},
        "mem": {"min": 10, "max": 100, "total": 1000, "count": 10, "current": 90},
    }
    row = cc.normalize_job(job)
    assert row["time_end"] == job["time_start"] + job["elapsed"]
    assert row["cpu_avg"] == 50
    assert row["mem_avg"] == 100
    assert row["python_script"] == "process_health.py"


def test_missing_cpu_mem_are_not_zero(tmp_path):
    db = tmp_path / "cronicle.sqlite3"
    cc.upsert_jobs([
        {"id": "empty", "time_start": 1788415380, "elapsed": 60, "event_title": "Empty", "cpu": {}, "mem": {}},
    ], db)
    with sqlite3.connect(db) as con:
        row = con.execute("select cpu_avg, cpu_max, mem_avg, mem_max from cronicle_jobs where job_id='empty'").fetchone()
    assert row == (None, None, None, None)
    result = cc.analyze_jobs_near_anomaly("2026-09-03T06:03:00Z", db_path=db, cpu_threshold=1, max_results=10)
    assert result["status"] == "NoJobsAboveThreshold"
    assert result["jobs"] == []


def test_analyzer_overlap_threshold_ranking_and_minimal_output(tmp_path):
    db = tmp_path / "cronicle.sqlite3"
    cc.upsert_jobs([
        {"id": "before", "time_start": 1788429000, "elapsed": 1200, "event_title": "LongJob", "command": "python a.py", "cpu": {"max": 60, "total": 240, "count": 4}, "mem": {}},
        {"id": "inside", "time_start": 1788429900, "elapsed": 60, "event_title": "HotJob", "params": {"cmd": "python b.py"}, "cpu": {"max": 90, "total": 300, "count": 5}, "mem": {}},
        {"id": "low", "time_start": 1788429900, "elapsed": 60, "event_title": "LowJob", "command": "python low.py", "cpu": {"max": 2, "total": 3, "count": 2}, "mem": {}},
        {"id": "outside", "time_start": 1788400000, "elapsed": 10, "event_title": "OutsideJob", "command": "python outside.py", "cpu": {"max": 99, "total": 99, "count": 1}, "mem": {}},
    ], db)
    result = cc.analyze_jobs_near_anomaly("2026-09-03T10:10:00Z", db_path=db, window_minutes=30, cpu_threshold=50, max_results=10)
    assert result["status"] == "Available"
    assert result["jobs"] == [
        {"scheduled_job": "HotJob", "python_script": "b.py"},
        {"scheduled_job": "LongJob", "python_script": "a.py"},
    ]
    assert "cpu_avg" not in json.dumps(result["jobs"])
    assert "job_id" not in json.dumps(result["jobs"])


def test_enriches_platform_mongo_points_only(tmp_path):
    db = tmp_path / "cronicle.sqlite3"
    cc.upsert_jobs([
        {"id": "j1", "time_start": 1788429000, "elapsed": 1200, "event_title": "PlatformJob", "command": "python platform.py", "cpu": {"max": 80, "total": 160, "count": 2}, "mem": {}},
    ], db)
    rows = [
        {
            "ResourceID": "Platform_MongoDb",
            "MongoDBResourceID": "Cluster_Platform",
            "HourlyHealthData": [{"Timestamp": "2026-09-03T10:00:00Z"}],
            "Metrics": {"Connections": [{"Timestamp": "2026-09-03T10:00:00Z", "Value": 1}]},
        },
        {
            "ResourceID": "Piaggio_MongoDb",
            "MongoDBResourceID": "Cluster_Piaggio",
            "HourlyHealthData": [{"Timestamp": "2026-09-03T10:00:00Z"}],
            "Metrics": {"Connections": [{"Timestamp": "2026-09-03T10:00:00Z", "Value": 1}]},
        },
    ]
    enriched, summary = cc.enrich_mongo_health_rows(rows, db_path=db, cpu_threshold=50)
    platform_point = enriched[0]["Metrics"]["Connections"][0]
    assert platform_point["CronicleJobs"] == [{"scheduled_job": "PlatformJob", "python_script": "platform.py"}]
    assert "CronicleJobs" not in enriched[1]["Metrics"]["Connections"][0]
    assert summary["point_count"] == 1
    assert summary["points_with_jobs"] == 1
