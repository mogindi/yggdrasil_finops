import datetime as dt
import unittest
from unittest.mock import patch

from cloudkitty_client import CloudKittyClient


class CloudKittyClientParsingTests(unittest.TestCase):
    def setUp(self):
        env = {
            "OS_AUTH_URL": "https://keystone.example/v3",
            "OS_USERNAME": "u",
            "OS_PASSWORD": "p",
            "OS_PROJECT_ID": "proj",
            "CLOUDKITTY_ENDPOINT": "https://ck.example",
            "OS_USER_DOMAIN_NAME": "Default",
            "OS_PROJECT_DOMAIN_NAME": "Default",
            "OS_INTERFACE": "public",
            "OS_VERIFY": "true",
        }
        self.env_patcher = patch.dict("os.environ", env, clear=True)
        self.env_patcher.start()

    def tearDown(self):
        self.env_patcher.stop()

    def test_aggregate_uses_rate_from_summary_payload(self):
        client = CloudKittyClient()
        with patch.object(client, "request", return_value={"summary": [{"rate": "0.02"}]}) as request_mock:
            total = client.get_project_aggregate_now("project-1")

        self.assertEqual(total, 0.02)
        self.assertEqual(request_mock.call_args.args[1], "/v1/report/summary")

    def test_time_series_extracts_begin_and_rate(self):
        client = CloudKittyClient()
        payload = {
            "summary": [
                {"begin": "2026-02-20T10:18:41", "rate": "0.02"},
                {"begin": "2026-02-21T10:18:41", "rate": "0.03"},
            ]
        }
        with patch.object(client, "request", return_value=payload) as request_mock:
            series = client.get_project_time_series(
                "project-1",
                dt.datetime(2026, 2, 20, tzinfo=dt.timezone.utc),
                dt.datetime(2026, 2, 21, tzinfo=dt.timezone.utc),
            )

        self.assertEqual(request_mock.call_args.args[1], "/v1/report/summary")

        self.assertEqual(series, [
            {"timestamp": "2026-02-20T10:18:41", "cost": 0.02},
            {"timestamp": "2026-02-21T10:18:41", "cost": 0.03},
        ])

    def test_get_project_created_at_parses_keystone_timestamp(self):
        client = CloudKittyClient()
        client._token = "token"
        with patch.object(client, "_http_json", return_value=(200, {}, {"project": {"created_at": "2026-01-15T05:00:00Z"}})):
            created_at = client.get_project_created_at("project-1")

        self.assertEqual(created_at, dt.datetime(2026, 1, 15, 5, 0, tzinfo=dt.timezone.utc))

    def test_default_hashmap_pricing_leaves_network_egress_unpriced(self):
        client = CloudKittyClient()

        with patch.object(client, "_get_or_create_service", return_value={"service_id": "svc-1"}) as service_mock, \
             patch.object(client, "_get_or_create_field", return_value={"field_id": "fld-1"}) as field_mock, \
             patch.object(client, "_ensure_mappings") as ensure_mock:
            summary = client.ensure_default_hashmap_pricing()

        self.assertEqual(service_mock.call_count, 3)
        self.assertEqual(field_mock.call_count, 3)
        self.assertEqual(ensure_mock.call_count, 3)

        services = {item["service"]: item["mappings"] for item in summary["services"]}
        self.assertEqual(services["volume"], [{"value": "__DEFAULT__", "cost": 0.08}])
        self.assertEqual(services["network.bw.out"], [])

    def test_default_hashmap_pricing_accepts_custom_configuration(self):
        client = CloudKittyClient()
        pricing = {"instance": [{"value": "x1", "cost": 0.123}]}

        with patch.object(client, "_get_or_create_service", return_value={"service_id": "svc-1"}), \
             patch.object(client, "_get_or_create_field", return_value={"field_id": "fld-1"}), \
             patch.object(client, "_ensure_mappings") as ensure_mock:
            summary = client.ensure_default_hashmap_pricing(pricing)

        ensure_mock.assert_called_once_with("fld-1", [{"value": "x1", "cost": 0.123}])
        self.assertEqual(summary["services"][0]["service"], "instance")


if __name__ == "__main__":
    unittest.main()
