import json
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import app
from cloudkitty_client import ProjectNotFoundError
from opensearch_client import OpenSearchApiError, OpenSearchClient


class FakeCloudKittyClient:
    def __init__(self, debug=False):
        pass

    def ensure_project_exists(self, project_id):
        if project_id == "missing-project":
            raise ProjectNotFoundError(f"Project '{project_id}' does not exist")


class FakeOpenSearchClient:
    upsert_calls = []

    def __init__(self, debug=False):
        self.endpoint = "http://fake-opensearch:9200"

    def search_project_payments(self, project_id, size=25):
        return {"hits": {"hits": []}}

    def upsert_payment_event(self, partition, event_id, document):
        self.__class__.upsert_calls.append((partition, event_id, document))
        return {"result": "created", "_id": event_id}


class OpenSearchPaymentsTests(unittest.TestCase):
    def _request(self, method, path, body=None):
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.CostHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            payload = json.dumps(body) if body is not None else None
            headers = {"Content-Type": "application/json"} if body is not None else {}
            conn.request(method, path, body=payload, headers=headers)
            resp = conn.getresponse()
            data = json.loads(resp.read().decode("utf-8"))
            conn.close()
            return resp.status, data
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_opensearch_url_is_configurable(self):
        with patch.dict("os.environ", {"OPENSEARCH_URL": "https://os.example:9443"}, clear=False):
            client = OpenSearchClient()
        self.assertEqual(client.endpoint, "https://os.example:9443")

    def test_debug_logs_opensearch_api_calls(self):
        client = OpenSearchClient(debug=True)

        class _Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b"{}"

        with patch("urllib.request.urlopen", return_value=_Resp()) as urlopen_mock, patch.object(client, "_debug") as debug_mock:
            client._http_json("GET", "/_cluster/health")

        self.assertTrue(urlopen_mock.called)
        debug_messages = [call.args[0] for call in debug_mock.call_args_list]
        self.assertTrue(any("OpenSearch API call" in msg for msg in debug_messages))
        self.assertTrue(any("OpenSearch API response" in msg for msg in debug_messages))

    def test_payments_template_uses_compatible_metadata_mapping(self):
        client = OpenSearchClient()
        with patch.object(client, "_http_json", return_value={"acknowledged": True}) as http_mock:
            client.create_payments_template()

        body = http_mock.call_args.args[2]
        metadata_mapping = body["template"]["mappings"]["properties"]["metadata"]
        self.assertEqual(metadata_mapping, {"type": "object", "enabled": False})

    def test_create_payments_index_uses_project_partition_name(self):
        client = OpenSearchClient()
        with patch.object(client, "_http_json", return_value={"acknowledged": True}) as http_mock:
            client.create_payments_index("project:Acme/West")

        self.assertEqual(http_mock.call_args.args[0], "PUT")
        self.assertEqual(http_mock.call_args.args[1], "/payments-project-acme-west")

    def test_create_payments_index_is_idempotent_when_index_exists(self):
        client = OpenSearchClient()
        exc_body = json.dumps(
            {
                "error": {
                    "type": "resource_already_exists_exception",
                    "root_cause": [{"type": "resource_already_exists_exception"}],
                }
            }
        )
        with patch.object(
            client,
            "_http_json",
            side_effect=OpenSearchApiError("OpenSearch request failed (400)", status_code=400, body=exc_body),
        ):
            payload = client.create_payments_index("project:proj-123")

        self.assertTrue(payload["acknowledged"])
        self.assertTrue(payload["already_exists"])

    def test_put_payment_event_injects_project_id(self):
        FakeOpenSearchClient.upsert_calls = []
        with patch("app.OpenSearchClient", FakeOpenSearchClient), patch("app.CloudKittyClient", FakeCloudKittyClient):
            status, body = self._request(
                "PUT",
                "/api/projects/proj-123/payments/events/evt_1",
                body={"event_id": "evt_1", "amount": 99.95, "status": "succeeded"},
            )

        self.assertEqual(status, 201)
        self.assertEqual(body["result"], "created")
        partition, event_id, doc = FakeOpenSearchClient.upsert_calls[0]
        self.assertEqual(partition, "project:proj-123")
        self.assertEqual(event_id, "evt_1")
        self.assertEqual(doc["project_id"], "proj-123")

    def test_payments_endpoints_return_404_when_cloudkitty_project_is_missing(self):
        with patch("app.OpenSearchClient", FakeOpenSearchClient), patch("app.CloudKittyClient", FakeCloudKittyClient):
            status, body = self._request("GET", "/api/projects/missing-project/payments")

        self.assertEqual(status, 404)
        self.assertIn("does not exist", body["error"])


if __name__ == "__main__":
    unittest.main()
