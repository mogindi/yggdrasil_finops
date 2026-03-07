import json
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import os
import sys
import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path
from urllib.error import HTTPError, URLError
from unittest.mock import patch, MagicMock


def _load_payments_app_module():
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    app_path = repo_root / "payments_app.py"
    loader = SourceFileLoader("payments_app", str(app_path))
    spec = importlib.util.spec_from_loader("payments_app", loader)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


payments_app = _load_payments_app_module()


class FakeOpenSearchClient:
    def __init__(self, debug=False):
        self.endpoint = "http://opensearch:9200"

    def get_total_paid(self, project_id, paid_before=None):
        return {"aggregations": {"total_paid": {"value": 10.0}}}

    def get_total_paid_in_range(self, project_id, created_from=None, created_to=None):
        return {"aggregations": {"total_paid": {"value": 10.0}}}

    def list_payments_created_in_range(self, project_id, created_from=None, created_to=None, size=500):
        return []


class TestPaymentsBalanceErrorHandling:
    def _request(self, method, path):
        server = ThreadingHTTPServer(("127.0.0.1", 0), payments_app.PaymentsHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            conn.request(method, path)
            resp = conn.getresponse()
            body = json.loads(resp.read().decode("utf-8"))
            conn.close()
            return resp.status, body
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_returns_502_when_costs_service_is_unavailable(self):
        with patch.dict(os.environ, {"OPENSEARCH_URL": "http://opensearch:9200", "OS_VERIFY": "false"}, clear=False), \
             patch.object(payments_app, "OpenSearchClient", FakeOpenSearchClient), \
             patch("payments_app.request.urlopen", side_effect=URLError("connection refused")):
            status, body = self._request("GET", "/api/projects/proj-123/payments/balance")

        assert status == 502
        assert body["error"] == "costs service unavailable: connection refused"

    def test_returns_404_when_costs_service_reports_missing_project(self):
        missing_resp = MagicMock()
        missing_resp.read.return_value = b'{"error":"Project \'proj-123\' does not exist"}'
        missing_exc = HTTPError(
            url="http://costs/api/projects/proj-123/costs",
            code=404,
            msg="Not Found",
            hdrs=None,
            fp=missing_resp,
        )
        with patch.dict(os.environ, {"OPENSEARCH_URL": "http://opensearch:9200", "OS_VERIFY": "false"}, clear=False), \
             patch.object(payments_app, "OpenSearchClient", FakeOpenSearchClient), \
             patch("payments_app.request.urlopen", side_effect=missing_exc):
            status, body = self._request("GET", "/api/projects/proj-123/payments/balance")

        assert status == 404
        assert body["error"] == "Project 'proj-123' does not exist"





    def test_balance_defaults_from_dates_to_2026_01_01_when_omitted(self):
        captured = {}

        def fake_compute(project_id, costs_from, costs_to, payments_from, payments_to):
            captured["project_id"] = project_id
            captured["costs_from"] = costs_from.isoformat()
            captured["payments_from"] = payments_from.isoformat()
            return {"ok": True}

        with patch.dict(os.environ, {"OPENSEARCH_URL": "http://opensearch:9200", "OS_VERIFY": "false"}, clear=False), \
             patch.object(payments_app, "OpenSearchClient", FakeOpenSearchClient), \
             patch.object(payments_app, "_compute_project_balance", side_effect=fake_compute):
            status, body = self._request("GET", "/api/projects/proj-123/payments/balance?costs_to_date=2026-02-15")

        assert status == 200
        assert body == {"ok": True}
        assert captured["project_id"] == "proj-123"
        assert captured["costs_from"] == "2026-01-01T00:00:00+00:00"
        assert captured["payments_from"] == "2026-01-01T00:00:00+00:00"

    def test_balance_defaults_from_dates_to_2026_01_01_with_as_of_date(self):
        captured = {}

        def fake_compute(project_id, costs_from, costs_to, payments_from, payments_to):
            captured["costs_from"] = costs_from.isoformat()
            captured["payments_from"] = payments_from.isoformat()
            captured["costs_to"] = costs_to.isoformat()
            captured["payments_to"] = payments_to.isoformat()
            return {"ok": True}

        with patch.dict(os.environ, {"OPENSEARCH_URL": "http://opensearch:9200", "OS_VERIFY": "false"}, clear=False), \
             patch.object(payments_app, "OpenSearchClient", FakeOpenSearchClient), \
             patch.object(payments_app, "_compute_project_balance", side_effect=fake_compute):
            status, body = self._request("GET", "/api/projects/proj-123/payments/balance?as_of_date=2026-02-01")

        assert status == 200
        assert body == {"ok": True}
        assert captured["costs_from"] == "2026-01-01T00:00:00+00:00"
        assert captured["payments_from"] == "2026-01-01T00:00:00+00:00"
        assert captured["costs_to"] == "2026-02-01T23:59:59+00:00"
        assert captured["payments_to"] == "2026-02-01T23:59:59+00:00"

    def test_parse_iso_date_defaults_to_start_of_day_utc(self):
        parsed = payments_app._parse_iso_date_or_datetime("2026-01-01")
        assert parsed.isoformat() == "2026-01-01T00:00:00+00:00"

    def test_parse_iso_date_to_date_uses_end_of_day_when_requested(self):
        parsed = payments_app._parse_iso_date_or_datetime("2026-01-01", end_of_day_for_date_only=True)
        assert parsed.isoformat() == "2026-01-01T23:59:59+00:00"
