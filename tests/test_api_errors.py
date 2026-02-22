import json
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import app
from cloudkitty_client import CloudKittyError, ProjectNotFoundError


class MissingProjectClient:
    def __init__(self, debug=False):
        pass

    def ensure_project_exists(self, project_id):
        raise ProjectNotFoundError(f"Project '{project_id}' does not exist")


class ExistingProjectDataFailureClient:
    def __init__(self, debug=False):
        pass

    def ensure_project_exists(self, project_id):
        return None

    def get_project_aggregate_for_range(self, project_id, start, end):
        raise CloudKittyError("Unable to obtain data for project 'existing-project'")


class ApiErrorHandlingTests(unittest.TestCase):
    def _request_costs(self, project_id):
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.CostHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            conn.request("GET", f"/api/projects/{project_id}/costs")
            resp = conn.getresponse()
            body = json.loads(resp.read().decode("utf-8"))
            conn.close()
            return resp.status, body
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_returns_404_when_project_does_not_exist(self):
        with patch("app.CloudKittyClient", MissingProjectClient):
            status, body = self._request_costs("missing-project")
        self.assertEqual(status, 404)
        self.assertIn("does not exist", body["error"])

    def test_returns_502_when_project_exists_but_data_unavailable(self):
        with patch("app.CloudKittyClient", ExistingProjectDataFailureClient):
            status, body = self._request_costs("existing-project")
        self.assertEqual(status, 502)
        self.assertIn("Unable to obtain data", body["error"])


if __name__ == "__main__":
    unittest.main()
