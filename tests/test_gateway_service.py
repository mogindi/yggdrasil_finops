import json
import threading
import unittest
import http.client
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import gateway_service


class GatewayProxyErrorHandlingTests(unittest.TestCase):
    def _request(self, path: str):
        server = ThreadingHTTPServer(("127.0.0.1", 0), gateway_service.GatewayHandler)
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

    def test_returns_502_when_upstream_closes_connection(self):
        old_costs_url = gateway_service.COSTS_SERVICE_URL
        gateway_service.COSTS_SERVICE_URL = "http://upstream.invalid"
        try:
            with patch("gateway_service.request.urlopen", side_effect=http.client.RemoteDisconnected("Remote end closed connection without response")):
                status, body = self._request("/api/projects/proj-123/costs")
        finally:
            gateway_service.COSTS_SERVICE_URL = old_costs_url

        self.assertEqual(status, 502)
        self.assertIn("upstream unavailable", body["error"])


if __name__ == "__main__":
    unittest.main()
