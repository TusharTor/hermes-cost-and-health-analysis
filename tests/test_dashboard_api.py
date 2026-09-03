import json
import tempfile
import unittest
from pathlib import Path

import dashboard_api
import publish_cloudvitals_plugin


def write_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def make_fixture(tmp_path: Path):
    ts = "010126_000000"
    cost = tmp_path / f"Cost-Analysis_{ts}.json"
    health = tmp_path / f"Health-Analysis_{ts}.json"
    azure_health = tmp_path / f"Azure_Health_Analysis_{ts}.json"
    mongo_health = tmp_path / "Mongo_Health_Analysis_01012026_000000.json"
    meta = tmp_path / f"Cost-Analysis_{ts}.meta.json"
    summary = tmp_path / f"Cost-Health-Summary_{ts}.json"
    timeseries = tmp_path / f"Health-Timeseries_{ts}.json"

    cost_rows = [
        {"ResourceID": "rid-a", "ResourceType": "Microsoft.Compute/virtualMachines", "AnalysisDate": "2026-05-01", "CostAmount": 10, "Severity": "Normal", "IsAnomaly": False, "TrendStatus": "Stable", "Trend": "Increasing", "DeviationPercentage": 0, "AnalysisReason": "normal"},
        {"ResourceID": "rid-a", "ResourceType": "Microsoft.Compute/virtualMachines", "AnalysisDate": "2026-05-02", "CostAmount": 30, "Severity": "Critical", "IsAnomaly": True, "TrendStatus": "Critical Cost Anomaly", "Trend": "Increasing", "DeviationPercentage": 200, "AnalysisReason": "spike"},
        {"ResourceID": "rid-b", "ResourceType": "Microsoft.Storage/storageAccounts", "AnalysisDate": "2026-05-01", "CostAmount": 7, "Severity": "Low", "IsAnomaly": True, "TrendStatus": "Cost Drop", "Trend": "Fluctuating", "DeviationPercentage": -20, "AnalysisReason": "drop"},
    ]
    health_rows = [
        {"ResourceID": "rid-a", "CostHealthCorrelation": "Correlation Observed", "OverallHealthStatus": "Warning", "CPUStatus": "Observed", "MemoryStatus": "Not Available", "DiskStatus": "Not Available", "NetworkStatus": "Not Available", "HealthAnalysisReason": "cpu high"},
        {"ResourceID": "rid-b", "CostHealthCorrelation": "Insufficient Data", "OverallHealthStatus": "Not Available", "CPUStatus": "Not Available", "MemoryStatus": "Not Available", "DiskStatus": "Not Available", "NetworkStatus": "Not Available", "HealthAnalysisReason": "none"},
    ]
    health_ts = [
        {"ResourceID": "rid-a", "Date": "2026-05-02", "MetricCategory": "CPU", "MetricName": "Percentage CPU", "Unit": "Percent", "Aggregation": "Average", "Points": [{"Timestamp": "2026-05-02T00:00:00Z", "Value": 20}, {"Timestamp": "2026-05-02T01:00:00Z", "Value": 80}]}
    ]
    azure_rows = [
        {"ResourceID": "rid-a", "ResourceType": "Microsoft.Compute/virtualMachines", "HealthSource": "AzureMonitor", "Metrics": {
            "CPU": [{"Timestamp": "2026-05-02T00:00:00Z", "Value": 44, "Unit": "Percent", "MetricName": "Percentage CPU", "Aggregation": "Average"}],
            "MemoryUsage": [{"Timestamp": "2026-05-02T00:00:00Z", "Value": 1024, "Unit": "Bytes", "MetricName": "Available Memory Bytes", "Aggregation": "Average"}],
            "Disk": [], "IOPs": [], "Network": [], "SNAT": []
        }}
    ]
    mongo_rows = [
        {"ResourceID": "rid-b", "MongoDBResource": "Platform_MongoDB", "HealthSource": "MongoAtlasCronJob", "Metrics": {
            "StorageSize": [{"Timestamp": "2026-05-01T00:00:00Z", "Value": 2048, "Unit": "MB", "MetricName": "cluster_metrics.memory.resident_mb", "Tier": "M50", "SlowQueryCount": 2}],
            "MemoryUsage": [{"Timestamp": "2026-05-01T00:00:00Z", "Value": 55.5, "Unit": "Percent", "MetricName": "cluster_metrics.memory.usage_percent", "Tier": "M50", "SlowQueryCount": 2}],
            "IndexSize": [{"Timestamp": "2026-05-01T00:00:00Z", "Value": 128, "Unit": "Bytes", "MetricName": "dbStats.total.indexSize"}],
            "LongRunningSlowQueries": [{"Timestamp": "2026-05-01T00:00:00Z", "Value": 2, "Unit": "Count", "MetricName": "currentOp.longRunningOperations"}],
            "Connections": [{"Timestamp": "2026-05-01T00:00:00Z", "Value": 22, "Unit": "Count", "MetricName": "cluster_metrics.connections.current", "Tier": "M50", "SlowQueryCount": 2}],
            "IOPs": [{"Timestamp": "2026-05-01T00:00:00Z", "Value": 99, "Unit": "Count", "MetricName": "opcounters.query"}]
        }}
    ]
    write_json(cost, cost_rows)
    write_json(health, health_rows)
    write_json(meta, {"fromDate": "2026-05-01", "toDate": "2026-05-31", "screening": [{"ResourceID": "rid-a", "IsAffected": True}, {"ResourceID": "rid-b", "IsAffected": True}]})
    write_json(timeseries, health_ts)
    write_json(azure_health, azure_rows)
    write_json(mongo_health, mongo_rows)
    write_json(summary, {
        "fromDate": "2026-05-01", "toDate": "2026-05-31", "cost_file": str(cost), "cost_meta_file": str(meta), "health_file": str(health), "health_timeseries_file": str(timeseries),
        "azure_health_analysis_file": str(azure_health), "mongo_health_analysis_file": str(mongo_health),
        "cost_documents_retrieved": 3, "resource_count": 2, "affected_resource_count": 2, "cost_anomaly_records": 2,
        "health_correlation_counts": {"Correlation Observed": 1, "Insufficient Data": 1}
    })
    return ts


class DashboardApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.ts = make_fixture(self.tmp_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_discover_runs_returns_summary_linked_files(self):
        runs = dashboard_api.discover_runs(self.tmp_path)
        self.assertEqual([r["run_id"] for r in runs], [self.ts])
        self.assertEqual(runs[0]["fromDate"], "2026-05-01")
        self.assertTrue(runs[0]["files"]["cost"].endswith(f"Cost-Analysis_{self.ts}.json"))
        self.assertTrue(runs[0]["files"]["azure_health"].endswith(f"Azure_Health_Analysis_{self.ts}.json"))
        self.assertTrue(runs[0]["files"]["mongo_health"].endswith("Mongo_Health_Analysis_01012026_000000.json"))
        self.assertTrue(runs[0]["has_health_timeseries"])
        self.assertTrue(runs[0]["has_split_health_analysis"])

    def test_affected_resources_are_ranked_by_severity_and_cost(self):
        resources = dashboard_api.affected_resources(self.tmp_path, self.ts)
        self.assertEqual([r["ResourceID"] for r in resources], ["rid-a", "rid-b"])
        self.assertEqual(resources[0]["MaxSeverity"], "Critical")
        self.assertEqual(resources[0]["AnomalyCount"], 1)
        self.assertEqual(resources[0]["PeakCost"], 30)

    def test_cost_timeseries_is_sorted_and_preserves_click_payload_fields(self):
        rows = dashboard_api.cost_timeseries(self.tmp_path, self.ts, "rid-a")
        self.assertEqual([r["AnalysisDate"] for r in rows], ["2026-05-01", "2026-05-02"])
        self.assertIs(rows[1]["IsAnomaly"], True)
        self.assertEqual(rows[1]["TrendStatus"], "Critical Cost Anomaly")

    def test_overall_cost_timeseries_sums_cost_amount_by_date(self):
        ts = "020126_000000"
        write_json(self.tmp_path / f"Cost-Analysis_{ts}.json", [
            {"ResourceID": "resource-a", "AnalysisDate": "2026-08-01", "CostAmount": 100, "AverageCost": 999},
            {"ResourceID": "resource-a", "AnalysisDate": "2026-08-02", "CostAmount": 200, "AverageCost": 999},
            {"ResourceID": "resource-b", "AnalysisDate": "2026-08-01", "CostAmount": 50, "Deviation": 999},
            {"ResourceID": "resource-b", "AnalysisDate": "2026-08-02", "CostAmount": 75, "PercentageChange": 999},
            {"ResourceID": "resource-b", "AnalysisDate": "2026-08-03", "CostAmount": "not-a-number"},
            {"ResourceID": "resource-c", "CostAmount": 25},
        ])
        write_json(self.tmp_path / f"Health-Analysis_{ts}.json", [])

        rows = dashboard_api.overall_cost_timeseries(self.tmp_path, ts)

        self.assertEqual(rows, [
            {"AnalysisDate": "2026-08-01", "CostAmount": 150.0},
            {"AnalysisDate": "2026-08-02", "CostAmount": 275.0},
        ])

    def test_overall_cost_endpoint_uses_existing_run_resolution(self):
        payload = dashboard_api.api_payload("/api/cost/overall", {"run_id": [self.ts]}, self.tmp_path)
        self.assertEqual(payload, [
            {"AnalysisDate": "2026-05-01", "CostAmount": 17.0},
            {"AnalysisDate": "2026-05-02", "CostAmount": 30.0},
        ])

    def test_cost_and_overall_timeseries_append_forecast_predictions(self):
        ts = "030126_000000"
        cost_path = self.tmp_path / f"Cost-Analysis_{ts}.json"
        health_path = self.tmp_path / f"Health-Analysis_{ts}.json"
        summary_path = self.tmp_path / f"Cost-Health-Summary_{ts}.json"
        write_json(cost_path, {
            "CostAnalysis": [
                {"ResourceID": "rid-a", "ResourceType": "Compute", "AnalysisDate": "2026-05-01", "CostAmount": 10, "Severity": "Normal", "TrendStatus": "Stable"},
                {"ResourceID": "rid-a", "ResourceType": "Compute", "AnalysisDate": "2026-05-02", "CostAmount": 20, "Severity": "High", "TrendStatus": "Cost Spike"},
                {"ResourceID": "rid-b", "ResourceType": "Storage", "AnalysisDate": "2026-05-01", "CostAmount": 5, "Severity": "Low", "TrendStatus": "Stable"},
            ],
            "Forecast": {
                "ForecastStart": "2026-05-03",
                "ForecastEnd": "2026-05-09",
                "ForecastDays": 7,
                "Overall": {
                    "DailyPredictions": [
                        {"Date": "2026-05-03", "PredictedCost": 31.5},
                        {"Date": "2026-05-04", "PredictedCost": 32.5},
                    ],
                    "Predicted7DayTotal": 220.0,
                },
                "AffectedResources": [
                    {
                        "ResourceID": "rid-a",
                        "ForecastModel": "RandomForestRegressor",
                        "DailyPredictions": [
                            {"Date": "2026-05-03", "PredictedCost": 22.25},
                            {"Date": "2026-05-04", "PredictedCost": 23.25},
                        ],
                    }
                ],
            },
        })
        write_json(health_path, [])
        write_json(summary_path, {"fromDate": "2026-05-01", "toDate": "2026-05-02", "cost_file": str(cost_path), "health_file": str(health_path)})

        resource_rows = dashboard_api.cost_timeseries(self.tmp_path, ts, "rid-a")
        overall_rows = dashboard_api.overall_cost_timeseries(self.tmp_path, ts)

        self.assertEqual([r["AnalysisDate"] for r in resource_rows], ["2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04"])
        self.assertEqual([r.get("IsPredicted", False) for r in resource_rows], [False, False, True, True])
        self.assertEqual(resource_rows[2]["PredictedCost"], 22.25)
        self.assertEqual(resource_rows[2]["ForecastModel"], "RandomForestRegressor")
        self.assertEqual(overall_rows, [
            {"AnalysisDate": "2026-05-01", "CostAmount": 15.0},
            {"AnalysisDate": "2026-05-02", "CostAmount": 20.0},
            {"AnalysisDate": "2026-05-03", "CostAmount": 31.5, "PredictedCost": 31.5, "IsPredicted": True, "PointType": "Predicted", "TrendStatus": "Predicted Cost", "Severity": "Normal", "AnalysisReason": "Forecasted cost point generated from the selected-period training data.", "ForecastStart": "2026-05-03", "ForecastEnd": "2026-05-09", "ForecastDays": 7},
            {"AnalysisDate": "2026-05-04", "CostAmount": 32.5, "PredictedCost": 32.5, "IsPredicted": True, "PointType": "Predicted", "TrendStatus": "Predicted Cost", "Severity": "Normal", "AnalysisReason": "Forecasted cost point generated from the selected-period training data.", "ForecastStart": "2026-05-03", "ForecastEnd": "2026-05-09", "ForecastDays": 7},
        ])

    def test_health_timeseries_prefers_split_azure_file_and_filters_resource_date(self):
        payload = dashboard_api.health_timeseries(self.tmp_path, self.ts, "rid-a", "2026-05-02")
        self.assertEqual(payload["source"], "Azure_Health_Analysis")
        self.assertEqual(payload["health_kind"], "azure")
        self.assertEqual([s["MetricCategory"] for s in payload["series"]], ["CPU", "MemoryUsage"])
        self.assertEqual(payload["series"][0]["Points"][0]["Value"], 44)

    def test_health_timeseries_uses_split_mongo_file_for_mongo_resource_date(self):
        payload = dashboard_api.health_timeseries(self.tmp_path, self.ts, "rid-b", "2026-05-01")
        self.assertEqual(payload["source"], "Mongo_Health_Analysis")
        self.assertEqual(payload["health_kind"], "mongodb")
        self.assertEqual([s["MetricCategory"] for s in payload["series"]], ["Connections", "MemoryUsage", "StorageSize"])
        self.assertEqual(payload["series"][0]["Points"][0]["Value"], 22)
        self.assertEqual(payload["series"][0]["Points"][0]["Tier"], "M50")
        self.assertEqual(payload["series"][0]["Points"][0]["SlowQueryCount"], 2)

    def test_health_timeseries_includes_nat_gateway_cost_justification_series(self):
        rid = "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Network/natGateways/nat-1"
        write_json(self.tmp_path / f"Azure_Health_Analysis_{self.ts}.json", [{
            "ResourceID": rid,
            "ResourceType": "Microsoft.Network/natGateways",
            "HealthSource": "AzureMonitor",
            "Metrics": {
                "CPU": [], "MemoryUsage": [], "Disk": [], "IOPs": [], "Network": [], "SNAT": [],
                "TrafficGiB": [{"Timestamp": "2026-05-02T00:00:00Z", "Value": 12.5, "Unit": "GiB", "MetricName": "ByteCount", "Aggregation": "Total"}],
                "AvgConn": [{"Timestamp": "2026-05-02T00:00:00Z", "Value": 42, "Unit": "Count", "MetricName": "TotalConnectionCount", "Aggregation": "Average"}],
                "SNATPeak": [{"Timestamp": "2026-05-02T00:00:00Z", "Value": 100, "Unit": "Count", "MetricName": "SNATConnectionCount", "Aggregation": "Maximum"}],
            },
        }])

        payload = dashboard_api.health_timeseries(self.tmp_path, self.ts, rid, "2026-05-02")

        self.assertEqual(payload["source"], "Azure_Health_Analysis")
        self.assertEqual(payload["health_kind"], "azure")
        self.assertEqual([s["MetricCategory"] for s in payload["series"]], ["TrafficGiB", "AvgConn", "SNATPeak"])
        self.assertEqual(payload["series"][0]["MetricName"], "ByteCount")
        self.assertEqual(payload["series"][0]["Unit"], "GiB")

    def test_health_timeseries_falls_back_to_summary_without_fabricating_points(self):
        (self.tmp_path / f"Health-Timeseries_{self.ts}.json").unlink()
        (self.tmp_path / f"Azure_Health_Analysis_{self.ts}.json").unlink()
        (self.tmp_path / "Mongo_Health_Analysis_01012026_000000.json").unlink()
        payload = dashboard_api.health_timeseries(self.tmp_path, self.ts, "rid-b", "2026-05-01")
        self.assertEqual(payload["source"], "Health-Analysis summary")
        self.assertEqual(payload["series"], [])
        self.assertEqual(payload["summary"]["CostHealthCorrelation"], "Insufficient Data")
        self.assertIn("No hourly health time-series file", payload["message"])

    def test_health_timeseries_explains_azure_retention_expiry_for_split_file(self):
        write_json(self.tmp_path / f"Azure_Health_Analysis_{self.ts}.json", [{
            "ResourceID": "rid-a",
            "ResourceType": "Microsoft.Compute/virtualMachines",
            "HealthSource": "AzureMonitor",
            "TemporalCoverage": "AzureMetricRetentionExpired",
            "Metrics": {"CPU": [], "MemoryUsage": [], "Disk": [], "IOPs": [], "Network": [], "SNAT": []},
            "MetricErrors": [{"MetricCategory": "CPU", "Error": "HTTP 400: Query endTime is out of the Max metrics retention period: 93.00:00:00"}],
        }])

        payload = dashboard_api.health_timeseries(self.tmp_path, self.ts, "rid-a", "2026-05-02")

        self.assertEqual(payload["source"], "Azure_Health_Analysis")
        self.assertEqual(payload["series"], [])
        self.assertIn("outside Azure Monitor platform metrics retention", payload["message"])

    def test_health_timeseries_falls_back_when_hourly_file_is_empty(self):
        (self.tmp_path / f"Azure_Health_Analysis_{self.ts}.json").unlink()
        (self.tmp_path / "Mongo_Health_Analysis_01012026_000000.json").unlink()
        write_json(self.tmp_path / f"Health-Timeseries_{self.ts}.json", [])
        payload = dashboard_api.health_timeseries(self.tmp_path, self.ts, "rid-a", "2026-05-02")
        self.assertEqual(payload["source"], "Health-Analysis summary")
        self.assertEqual(payload["series"], [])
        self.assertIn("0 usable series", payload["message"])

    def test_prefixed_route_normalization_for_tor_ops_agent_dashboard(self):
        self.assertEqual(dashboard_api.normalize_request_path("/tor-ops-agent/dashboard"), ("/index.html", True))
        self.assertEqual(dashboard_api.normalize_request_path("/tor-ops-agent/dashboard/"), ("/index.html", True))
        self.assertEqual(dashboard_api.normalize_request_path("/tor-ops-agent/dashboard/app.js"), ("/app.js", True))
        self.assertEqual(dashboard_api.normalize_request_path("/tor-ops-agent/dashboard/api/runs"), ("/api/runs", True))
        self.assertEqual(dashboard_api.normalize_request_path("/api/runs"), ("/api/runs", False))

    def test_publish_static_payload_includes_overall_cost_without_removing_existing_data(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            plugin = Path(td) / "plugin" / "dashboard"
            data_dir = Path(td) / "data"
            (root / "static").mkdir(parents=True)
            plugin.mkdir(parents=True)
            data_dir.mkdir(parents=True)
            (root / "static" / "styles.css").write_text("body{}", encoding="utf-8")
            (root / "static" / "app.js").write_text("console.log('app')", encoding="utf-8")
            (root / "static" / "index.html").write_text('<link rel="stylesheet" href="/styles.css"><script src="/app.js"></script>', encoding="utf-8")
            ts = make_fixture(data_dir)
            old_root, old_plugin, old_data = publish_cloudvitals_plugin.ROOT, publish_cloudvitals_plugin.PLUGIN, publish_cloudvitals_plugin.DATA_DIR
            try:
                publish_cloudvitals_plugin.ROOT = root
                publish_cloudvitals_plugin.PLUGIN = plugin
                publish_cloudvitals_plugin.DATA_DIR = data_dir
                result = publish_cloudvitals_plugin.publish(ts)
            finally:
                publish_cloudvitals_plugin.ROOT = old_root
                publish_cloudvitals_plugin.PLUGIN = old_plugin
                publish_cloudvitals_plugin.DATA_DIR = old_data

            self.assertTrue(result["ok"])
            self.assertTrue((plugin / "dist" / "app.js").is_file())
            self.assertTrue((plugin / "dist" / "cloudvitals.html").is_file())
            html = (plugin / "dist" / "cloudvitals.html").read_text(encoding="utf-8")
            self.assertIn(f'href="styles.css?v={ts}"', html)
            self.assertIn(f'src="data.js?v={ts}"', html)
            self.assertIn(f'src="app.js?v={ts}"', html)
            data_js = (plugin / "dist" / "data.js").read_text(encoding="utf-8")
            payload = json.loads(data_js.removeprefix("window.CLOUDVITALS_STATIC_DATA = ").removesuffix(";\n"))
            self.assertIn("overallCost", payload)
            self.assertIn(ts, payload["overallCost"])
            self.assertEqual(payload["overallCost"][ts], [
                {"AnalysisDate": "2026-05-01", "CostAmount": 17.0},
                {"AnalysisDate": "2026-05-02", "CostAmount": 30.0},
            ])
            self.assertIn(ts, payload["resources"])
            self.assertIn(ts, payload["cost"])
            self.assertIn(ts, payload["healthSummary"])
            self.assertIn(ts, payload["healthIndex"])
            self.assertIn(ts, payload["healthCoverage"])
            self.assertNotIn("azureHealth", payload)
            self.assertNotIn("mongoHealth", payload)
            self.assertNotIn("healthSeries", payload)
            azure_entry = payload["healthIndex"][ts]["rid-a|2026-05-02"]
            mongo_entry = payload["healthIndex"][ts]["rid-b|2026-05-01"]
            self.assertEqual(azure_entry["health_kind"], "azure")
            self.assertEqual(mongo_entry["health_kind"], "mongodb")
            azure_shard = json.loads((plugin / "dist" / azure_entry["file"]).read_text(encoding="utf-8"))
            mongo_shard = json.loads((plugin / "dist" / mongo_entry["file"]).read_text(encoding="utf-8"))
            self.assertEqual(azure_shard["series"][0]["MetricCategory"], "CPU")
            self.assertEqual([s["MetricCategory"] for s in mongo_shard["series"]], ["Connections", "MemoryUsage", "StorageSize"])
            self.assertEqual(mongo_shard["series"][0]["Points"][0]["Tier"], "M50")


if __name__ == "__main__":
    unittest.main()
