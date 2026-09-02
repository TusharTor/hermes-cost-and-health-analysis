# CloudVitals Cost + Health Dashboard

Interactive dashboard for Cost and Health Analysis Agent outputs stored in `/opt/data`.

## What it does

- Discovers `Cost-Health-Summary_*.json` runs in `/opt/data`.
- Shows affected-resource summary cards and severity/correlation context.
- Starts with an aggregate daily cost overview across the resources represented in the selected Cost-Analysis run.
- Lets the user select an affected resource to inspect that resource's daily cost trend.
- Lets the user click any resource-level daily cost point.
- Shows the corresponding resource/day health drilldown:
  - Uses `Health-Timeseries_<run_id>.json` when available.
  - Falls back to `Health-Analysis_<run_id>.json` summary when hourly data is not available.
  - Never fabricates hourly points.

## Run locally

```bash
cd /opt/data/cost-health-dashboard
python3 dashboard_api.py --host 127.0.0.1 --port 8765 --data-dir /opt/data
```

Open:

```text
http://127.0.0.1:8765/tor-ops-agent/dashboard
```

The standalone server also still serves `/` for local development. The dashboard reads `/opt/data` live on every API request, so new Cost/Health Agent output appears without republishing a Hermes plugin.

## Route prefix

CloudVitals is a standalone project and intentionally does **not** register as a Hermes dashboard plugin. Its stable app path is:

```text
/tor-ops-agent/dashboard
```

When exposed through a reverse proxy, forward that path to the standalone CloudVitals process, for example:

```text
/tor-ops-agent/dashboard/* -> http://127.0.0.1:8765/tor-ops-agent/dashboard/*
```

## API

```text
GET /api/runs
GET /api/summary?run_id=<id|latest>
GET /api/resources?run_id=<id|latest>
GET /api/cost/overall?run_id=<id|latest>
GET /api/cost?run_id=<id|latest>&resource_id=<ResourceID>
GET /api/health?run_id=<id|latest>&resource_id=<ResourceID>&date=YYYY-MM-DD
```

`/api/cost/overall` groups the selected run's Cost-Analysis rows by `AnalysisDate` and sums only `CostAmount` across the resources present in that analysis output. It does not sum averages, deviations, or percentages.

## Optional hourly health time-series generation

For a better hackathon demo, generate hourly Azure Monitor metrics for top affected ARM resources:

```bash
cd /opt/data/cost-health-dashboard
python3 generate_health_timeseries.py --run-id 310826_092145 --limit 20 --max-metrics 8
```

For MongoDB resources, hourly health graphs must be based on command snapshots collected as time passes. The collector uses only MongoDB commands (`serverStatus`, `hostInfo`, `list_database_names()`, `dbStats`, `$currentOp`) and does not read cached health collections.

Collect one current hourly snapshot for configured MongoDB workload keys:

```bash
cd /opt/data/cost-health-dashboard
COST_HEALTH_PWD='<password>' python3 mongodb_command_timeseries.py collect
```

Build dashboard-readable MongoDB series for a Cost/Health run from stored snapshots:

```bash
COST_HEALTH_PWD='<password>' python3 mongodb_command_timeseries.py build-run --run-id latest
```

This writes or updates:

```text
/opt/data/MongoDB-Command-Snapshots.jsonl
/opt/data/Health-Timeseries_<run_id>.json
```

The dashboard already reads `Health-Timeseries_<run_id>.json`; when a MongoDB resource/day has hourly command snapshots, clicking that day's cost point shows the full-day MongoDB health metric graph. Missing historical hours are not fabricated.

Dry-run without credentials:

```bash
python3 generate_health_timeseries.py --run-id latest --limit 20 --dry-run
```

Output file:

```text
/opt/data/Health-Timeseries_<run_id>.json
```

Schema:

```json
[
  {
    "ResourceID": "/subscriptions/.../providers/...",
    "ResourceType": "Microsoft.Compute/virtualMachines",
    "Date": "2026-05-14",
    "MetricCategory": "CPU",
    "MetricName": "Percentage CPU",
    "Unit": "Percent",
    "Aggregation": "Average",
    "Points": [
      {"Timestamp": "2026-05-14T00:00:00Z", "Value": 42.5}
    ]
  }
]
```

## Tests

```bash
cd /opt/data/cost-health-dashboard
python3 -m unittest discover -s tests -v
node tests/frontend_smoke.test.js
```
