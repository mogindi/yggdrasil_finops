import json
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import app


class RecordingClient:
    aggregate_calls = []
    series_calls = []

    def __init__(self, debug=False):
        pass

    def ensure_project_exists(self, project_id):
        return None

    def get_project_aggregate_for_range(self, project_id, start, end):
        self.__class__.aggregate_calls.append((project_id, start, end))
        return 42.5

    def get_project_time_series(self, project_id, start, end, resolution):
        self.__class__.series_calls.append((project_id, start, end, resolution))
        if resolution == "month":
            now = app.dt.datetime.now(app.dt.timezone.utc)
            current_month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            previous_month_start = (current_month_start - app.dt.timedelta(seconds=1)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            return [
                {"timestamp": previous_month_start.isoformat(), "cost": 5.0},
                {"timestamp": current_month_start.isoformat(), "cost": 9.0},
            ]
        return [{"timestamp": start.isoformat(), "cost": 1.0}, {"timestamp": end.isoformat(), "cost": 2.0}]


class LastMonthEndpointTests(unittest.TestCase):
    def setUp(self):
        RecordingClient.aggregate_calls = []
        RecordingClient.series_calls = []

    def _request(self, path):
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.CostHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            conn.request("GET", path)
            resp = conn.getresponse()
            body = json.loads(resp.read().decode("utf-8"))
            conn.close()
            return resp.status, body
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_last_month_endpoint_sets_previous_month_range(self):
        with patch("app.CloudKittyClient", RecordingClient):
            status, body = self._request("/api/projects/proj-1/costs/last-month")

        self.assertEqual(status, 200)
        self.assertEqual(body["project_id"], "proj-1")
        self.assertEqual(body["aggregate_cost_now"], 42.5)
        self.assertIn("start", body)
        self.assertIn("end", body)
        self.assertIn("time_series", body)

        _, start, end = RecordingClient.aggregate_calls[0]
        self.assertEqual(start.day, 1)
        self.assertEqual(start.hour, 0)
        self.assertEqual(start.minute, 0)
        self.assertEqual(start.second, 0)
        self.assertEqual(end.hour, 23)
        self.assertEqual(end.minute, 59)
        self.assertEqual(end.second, 59)
        self.assertEqual((end + app.dt.timedelta(seconds=1)).day, 1)
        self.assertEqual((end + app.dt.timedelta(seconds=1)).hour, 0)
        self.assertEqual((end + app.dt.timedelta(seconds=1)).minute, 0)
        self.assertEqual((end + app.dt.timedelta(seconds=1)).second, 0)
        self.assertEqual((end + app.dt.timedelta(seconds=1)).month, start.month % 12 + 1)

    def test_last_month_endpoint_respects_query_options(self):
        with patch("app.CloudKittyClient", RecordingClient):
            status, body = self._request("/api/projects/proj-2/costs/last-month?include_series=false&resolution=hour")

        self.assertEqual(status, 200)
        self.assertEqual(body["project_id"], "proj-2")
        self.assertEqual(body["resolution"], "hour")
        self.assertEqual(body["time_series"], [])
        self.assertEqual(len(RecordingClient.series_calls), 0)

    def test_specific_month_endpoint_sets_requested_month_range(self):
        with patch("app.CloudKittyClient", RecordingClient):
            status, body = self._request("/api/projects/proj-3/costs/2025-01?resolution=day")

        self.assertEqual(status, 200)
        self.assertEqual(body["project_id"], "proj-3")
        self.assertEqual(body["start"], "2025-01-01T00:00:00+00:00")
        self.assertEqual(body["end"], "2025-01-31T23:59:59+00:00")

        _, start, end = RecordingClient.aggregate_calls[0]
        self.assertEqual(start.isoformat(), "2025-01-01T00:00:00+00:00")
        self.assertEqual(end.isoformat(), "2025-01-31T23:59:59+00:00")

    def test_specific_month_endpoint_rejects_invalid_format(self):
        with patch("app.CloudKittyClient", RecordingClient):
            status, body = self._request("/api/projects/proj-4/costs/2025-13")

        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "Month must be in YYYY-MM format")

    def test_monthly_endpoint_returns_past_months_only_with_single_series_call(self):
        with patch("app.CloudKittyClient", RecordingClient):
            status, body = self._request("/api/projects/proj-5/costs/monthly")

        self.assertEqual(status, 200)
        self.assertEqual(body["project_id"], "proj-5")
        self.assertEqual(body["resolution"], "month")
        self.assertEqual(body["aggregate_cost_now"], 5.0)
        self.assertEqual(len(body["time_series"]), 1)

        self.assertEqual(len(RecordingClient.aggregate_calls), 0)
        self.assertEqual(len(RecordingClient.series_calls), 1)
        _, start, end, resolution = RecordingClient.series_calls[0]
        self.assertEqual(resolution, "month")
        self.assertEqual(start.isoformat(), "1970-01-01T00:00:00+00:00")
        self.assertEqual((end + app.dt.timedelta(seconds=1)).day, 1)
        self.assertEqual((end + app.dt.timedelta(seconds=1)).hour, 0)
        self.assertEqual((end + app.dt.timedelta(seconds=1)).minute, 0)
        self.assertEqual((end + app.dt.timedelta(seconds=1)).second, 0)


if __name__ == "__main__":
    unittest.main()
