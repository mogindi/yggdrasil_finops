import json
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import os
from unittest.mock import patch

import payments_app
from cloudkitty_client import OpenStackAuthError


class MissingAuthCloudKittyClient:
    def __init__(self, debug=False):
        raise OpenStackAuthError("OS_AUTH_URL is required")


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

    def test_returns_502_for_missing_openstack_auth_configuration(self):
        with patch.dict(os.environ, {"OPENSEARCH_URL": "http://opensearch:9200", "OS_VERIFY": "false"}, clear=False), \
             patch("payments_app.CloudKittyClient", MissingAuthCloudKittyClient):
            status, body = self._request("GET", "/api/projects/proj-123/payments/balance")

        assert status == 502
        assert body["error"] == "OS_AUTH_URL is required"
