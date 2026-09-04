import unittest

import generate_split_health_analysis as gen


class SplitHealthAnalysisGeneratorTests(unittest.TestCase):
    def test_mongo_stamp_uses_four_digit_year_filename(self):
        self.assertEqual(gen.mongo_run_id_from_run_id("010926_085615"), "01092026_085615")

    def test_mongo_mapping_does_not_classify_non_mongo_azure_resources_as_mongo(self):
        creds = {"Piaggio_MongoDB": "mongodb://example.invalid/db"}
        nat = "/subscriptions/s/resourceGroups/piaggio/providers/Microsoft.Network/natGateways/piaggio-ri-natgw"
        metric_alert = "/subscriptions/s/resourceGroups/piaggio/providers/Microsoft.Insights/metricAlerts/azuredocumentdb-memory"
        cluster = "/subscriptions/s/resourceGroups/piaggio/providers/Microsoft.DocumentDB/mongoClusters/tazuremongo-piaggio"
        self.assertEqual(gen.resolve_mongodb_credential_key(creds, nat), (None, None))
        self.assertEqual(gen.resolve_mongodb_credential_key(creds, metric_alert), (None, None))
        self.assertEqual(gen.resolve_mongodb_credential_key(creds, cluster)[0], "Piaggio_MongoDB")

    def test_mongo_metrics_from_snapshots_maps_required_categories(self):
        snapshots = [{
            "Timestamp": "2026-08-17T03:12:00Z",
            "Hour": "2026-08-17T03:00:00Z",
            "Metrics": {
                "dbStats.total.storageSize": {"Value": 2048, "Unit": "Bytes", "MetricCategory": "MongoDBStorage"},
                "dbStats.total.indexSize": {"Value": 128, "Unit": "Bytes", "MetricCategory": "MongoDBStorage"},
                "currentOp.longRunningOperations": {"Value": 2, "Unit": "Count", "MetricCategory": "MongoDBOperations"},
                "connections.current": {"Value": 18, "Unit": "Count", "MetricCategory": "MongoDBConnections"},
                "opcounters.query": {"Value": 40, "Unit": "Count", "MetricCategory": "MongoDBOperations"},
                "opcounters.insert": {"Value": 5, "Unit": "Count", "MetricCategory": "MongoDBOperations"}
            }
        }]

        metrics = gen.mongo_metrics_from_snapshots(snapshots)

        self.assertEqual(metrics["StorageSize"][0]["Value"], 2048)
        self.assertEqual(metrics["IndexSize"][0]["Value"], 128)
        self.assertEqual(metrics["LongRunningSlowQueries"][0]["Value"], 2)
        self.assertEqual(metrics["Connections"][0]["Value"], 18)
        self.assertEqual(metrics["IOPs"][0]["Value"], 45)
        self.assertEqual(metrics["IOPs"][0]["MetricName"], "opcounters.total")

    def test_azure_metric_retention_errors_are_detected(self):
        self.assertTrue(gen.is_azure_metric_retention_error("Query endTime is out of the Max metrics retention period: 93.00:00:00"))
        self.assertFalse(gen.is_azure_metric_retention_error("HTTP 401: InvalidAuthenticationTokenTenant"))

    def test_resource_subscription_id_extracts_subscription_segment(self):
        rid = "/subscriptions/7e4839cb-b952-43d0-9e46-bf16cce1e71b/resourceGroups/rg/providers/Microsoft.Web/sites/app"
        self.assertEqual(gen.resource_subscription_id(rid), "7e4839cb-b952-43d0-9e46-bf16cce1e71b")
        self.assertIsNone(gen.resource_subscription_id("Platform_MongoDb"))

    def test_configured_subscription_id_reads_credential_field(self):
        self.assertEqual(gen.configured_subscription_id({"subscription_id": "sub-1"}), "sub-1")
        self.assertEqual(gen.configured_subscription_id({"SubscriptionId": "sub-2"}), "sub-2")

    def test_azure_profiles_are_discovered_from_nested_sections(self):
        creds = {
            "AzureAd": {
                "Instance": "https://login.microsoftonline.com/",
                "TenantId": "tenant-a",
                "ClientId": "client-a",
                "ClientSecret": "secret-a",
                "SubscriptionId": "sub-a",
            },
            "DevTorAd": {
                "TenantId": "tenant-b",
                "ClientId": "client-b",
                "ClientSecret": "secret-b",
                "SubscriptionId": "sub-b",
            },
        }
        profiles = {p["subscription_id"]: p["ProfileName"] for p in gen.azure_credential_profiles(creds)}
        self.assertEqual(profiles["sub-a"], "AzureAd")
        self.assertEqual(profiles["sub-b"], "DevTorAd")

    def test_explicit_nested_profile_wins_over_legacy_duplicate_subscription(self):
        creds = {
            "tenant_id": "legacy-tenant",
            "client_id": "legacy-client",
            "client_secret": "legacy-secret",
            "subscription_id": "sub-a",
            "AzureAd": {
                "TenantId": "tenant-a",
                "ClientId": "client-a",
                "ClientSecret": "secret-a",
                "SubscriptionId": "sub-a",
            },
        }
        profile = gen.azure_credential_profiles(creds)[0]
        self.assertEqual(profile["ProfileName"], "AzureAd")
        self.assertEqual(profile["tenant_id"], "tenant-a")
    def test_choose_azure_metrics_prefers_percentage_cpu_and_memory(self):
        defs = [
            {"name": {"value": "CpuTime"}, "unit": "Seconds", "primaryAggregationType": "Total"},
            {"name": {"value": "CpuPercentage"}, "unit": "Percent", "primaryAggregationType": "Average"},
            {"name": {"value": "MemoryWorkingSet"}, "unit": "Bytes", "primaryAggregationType": "Average"},
            {"name": {"value": "MemoryPercentage"}, "unit": "Percent", "primaryAggregationType": "Average"},
        ]
        chosen = gen.choose_azure_metrics(defs, per_category=1)
        self.assertEqual(chosen["CPU"][0]["MetricName"], "CpuPercentage")
        self.assertEqual(chosen["MemoryUsage"][0]["MetricName"], "MemoryPercentage")

    def test_query_metric_can_call_pt6h_average_only(self):
        captured = {}
        def fake_http_json(method, url, headers=None, data=None, timeout=45):
            captured['url'] = url
            return {"value": [{"unit": "Percent", "timeseries": [{"data": [{"timeStamp": "2026-08-01T00:00:00Z", "average": 42.5}]}]}]}
        old_http_json = gen.http_json
        try:
            gen.http_json = fake_http_json
            points, err = gen.query_metric("/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm", "Percentage CPU", "token", "2026-08-01", "2026-08-31", preferred_aggregation="Average", interval="PT6H", aggregation_param="Average")
        finally:
            gen.http_json = old_http_json

        self.assertIsNone(err)
        self.assertIn("interval=PT6H", captured['url'])
        self.assertIn("aggregation=Average", captured['url'])
        self.assertEqual(points[0]["Aggregation"], "Average")
        self.assertEqual(points[0]["Granularity"], "PT6H")
        self.assertEqual(points[0]["Value"], 42.5)


if __name__ == "__main__":
    unittest.main()
