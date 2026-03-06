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
             patch("payments_app.OpenSearchClient", FakeOpenSearchClient), \
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
             patch("payments_app.OpenSearchClient", FakeOpenSearchClient), \
             patch("payments_app.request.urlopen", side_effect=missing_exc):
            status, body = self._request("GET", "/api/projects/proj-123/payments/balance")

        assert status == 404
        assert body["error"] == "Project 'proj-123' does not exist"
