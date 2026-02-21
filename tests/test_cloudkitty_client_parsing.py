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


if __name__ == "__main__":
    unittest.main()
