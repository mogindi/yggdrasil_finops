import io
import json
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError

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


    def test_extract_error_reason_from_root_cause(self):
        reason = OpenSearchClient._extract_error_reason(json.dumps({"error": {"root_cause": [{"reason": "index missing"}]}}))
        self.assertEqual(reason, "index missing")

    def test_http_json_error_message_includes_method_url_and_reason(self):
        with patch.dict("os.environ", {"OPENSEARCH_URL": "http://opensearch:9200", "OS_VERIFY": "false"}, clear=False):
            client = OpenSearchClient(debug=True)

        http_error = HTTPError(
            url="http://opensearch:9200/payments-*/_search",
            code=400,
            msg="Bad Request",
            hdrs=None,
            fp=io.BytesIO(b'{"error": {"reason": "query malformed"}}'),
        )

        with patch("urllib.request.urlopen", side_effect=http_error):
            with self.assertRaises(OpenSearchApiError) as exc_ctx:
                client._http_json("GET", "/payments-*/_search", {"query": {}})

        message = str(exc_ctx.exception)
        self.assertIn("method=GET", message)
        self.assertIn("url=http://opensearch:9200/payments-*/_search", message)
        self.assertIn("query malformed", message)

    def test_payments_template_uses_compatible_metadata_mapping(self):
        client = OpenSearchClient()
        with patch.object(client, "_http_json", return_value={"acknowledged": True}) as http_mock:
            client.create_payments_template()

        body = http_mock.call_args.args[2]
        method_mapping = body["template"]["mappings"]["properties"]["method"]
        reference_mapping = body["template"]["mappings"]["properties"]["reference"]
        metadata_mapping = body["template"]["mappings"]["properties"]["metadata"]
        self.assertEqual(method_mapping, {"type": "keyword"})
        self.assertEqual(reference_mapping, {"type": "keyword"})
        self.assertEqual(metadata_mapping, {"type": "object", "enabled": False})


    def test_upsert_payment_event_backfills_method_mapping_on_strict_mapping_error(self):
        client = OpenSearchClient()
        dynamic_exc = OpenSearchApiError(
            "OpenSearch request failed (400) method=PUT url=http://opensearch:9200/payments-project-proj-123/_doc/evt_1: "
            "mapping set to strict, dynamic introduction of [method] within [_doc] is not allowed",
            status_code=400,
        )
        with patch.object(
            client,
            "_http_json",
            side_effect=[dynamic_exc, {"acknowledged": True}, {"result": "created"}],
        ) as http_mock:
            payload = client.upsert_payment_event("project:proj-123", "evt_1", {"method": "card"})

        self.assertEqual(payload["result"], "created")
        self.assertEqual(http_mock.call_args_list[1].args[1], "/payments-project-proj-123/_mapping")
        self.assertEqual(http_mock.call_args_list[1].args[2], {"properties": {"method": {"type": "keyword"}}})

    def test_bulk_payment_events_backfills_method_mapping_on_strict_mapping_error(self):
        client = OpenSearchClient()
        dynamic_exc = OpenSearchApiError(
            "OpenSearch bulk request failed (400) method=POST url=http://opensearch:9200/_bulk: "
            "mapping set to strict, dynamic introduction of [method] within [_doc] is not allowed",
            status_code=400,
        )
        with patch.object(client, "_http_ndjson", side_effect=[dynamic_exc, {"errors": False}]) as ndjson_mock, patch.object(
            client,
            "_http_json",
            return_value={"acknowledged": True},
        ) as http_mock:
            payload = client.bulk_payment_events([{"event_id": "evt_1", "method": "card"}], "project:proj-123")

        self.assertEqual(payload["errors"], False)
        self.assertEqual(http_mock.call_args.args[1], "/payments-project-proj-123/_mapping")
        self.assertEqual(ndjson_mock.call_count, 2)

    def test_upsert_payment_event_backfills_reference_mapping_on_strict_mapping_error(self):
        client = OpenSearchClient()
        dynamic_exc = OpenSearchApiError(
            "OpenSearch request failed (400) method=PUT url=http://opensearch:9200/payments-project-proj-123/_doc/evt_1: "
            "mapping set to strict, dynamic introduction of [reference] within [_doc] is not allowed",
            status_code=400,
        )
        with patch.object(
            client,
            "_http_json",
            side_effect=[dynamic_exc, {"acknowledged": True}, {"result": "created"}],
        ) as http_mock:
            payload = client.upsert_payment_event("project:proj-123", "evt_1", {"reference": "wire-001"})

        self.assertEqual(payload["result"], "created")
        self.assertEqual(http_mock.call_args_list[1].args[1], "/payments-project-proj-123/_mapping")
        self.assertEqual(http_mock.call_args_list[1].args[2], {"properties": {"reference": {"type": "keyword"}}})


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



    def test_get_balance_returns_default_payload_when_missing(self):
        client = OpenSearchClient()
        missing_exc = OpenSearchApiError(
            "OpenSearch request failed (404) method=GET url=http://opensearch:9200/project-balances/_doc/proj-123: Not Found",
            status_code=404,
        )
        with patch.object(client, "_http_json", side_effect=missing_exc):
            payload = client.get_balance("proj-123")

        self.assertFalse(payload["found"])
        self.assertEqual(payload["_source"]["project_id"], "proj-123")
        self.assertEqual(payload["_source"]["balance"], 0.0)

    def test_get_balance_returns_default_payload_when_404_is_only_in_message(self):
        client = OpenSearchClient()
        missing_exc = OpenSearchApiError(
            "OpenSearch request failed (404) method=GET url=http://opensearch:9200/project-balances/_doc/proj-123: Not Found",
        )
        with patch.object(client, "_http_json", side_effect=missing_exc):
            payload = client.get_balance("proj-123")

        self.assertFalse(payload["found"])
        self.assertEqual(payload["_source"]["project_id"], "proj-123")
        self.assertEqual(payload["_source"]["balance"], 0.0)

    def test_search_project_payments_returns_empty_when_index_missing(self):
        client = OpenSearchClient()
        missing_exc = OpenSearchApiError(
            "OpenSearch request failed (404) method=GET url=http://opensearch:9200/payments-*/_search: no such index [payments-project-proj-123]",
            status_code=404,
            body=json.dumps({"error": {"type": "index_not_found_exception", "reason": "no such index"}}),
        )
        with patch.object(client, "_http_json", side_effect=missing_exc):
            payload = client.search_project_payments("proj-123")

        self.assertEqual(payload["hits"]["hits"], [])
        self.assertEqual(payload["hits"]["total"]["value"], 0)

    def test_get_index_mapping_returns_not_found_envelope_when_missing(self):
        client = OpenSearchClient()
        missing_exc = OpenSearchApiError(
            "OpenSearch request failed (404) method=GET url=http://opensearch:9200/payments-project-proj-123/_mapping: no such index",
            status_code=404,
        )
        with patch.object(client, "_http_json", side_effect=missing_exc):
            payload = client.get_index_mapping("project:proj-123")

        self.assertFalse(payload["found"])
        self.assertEqual(payload["index"], "payments-project-proj-123")
        self.assertEqual(payload["mappings"], {})

    def test_upsert_balance_computes_cost_minus_payments(self):
        client = OpenSearchClient()
        with patch.object(client, "_http_json", return_value={"result": "updated"}) as http_mock:
            client.upsert_balance("proj-123", "USD", costs_total=120.0, payments_total=150.0)

        self.assertEqual(http_mock.call_args.args[0], "POST")
        self.assertEqual(http_mock.call_args.args[1], "/project-balances/_update/proj-123")
        payload = http_mock.call_args.args[2]
        self.assertEqual(payload["doc"]["costs_total"], 120.0)
        self.assertEqual(payload["doc"]["payments_total"], 150.0)
        self.assertEqual(payload["doc"]["balance"], -30.0)


    def test_upsert_balance_backfills_mapping_on_strict_dynamic_error(self):
        client = OpenSearchClient()
        dynamic_exc = OpenSearchApiError(
            "OpenSearch request failed (400) method=POST url=http://opensearch:9200/project-balances/_update/proj-123: "
            "mapping set to strict, dynamic introduction of [costs_total] within [_doc] is not allowed",
            status_code=400,
        )
        with patch.object(
            client,
            "_http_json",
            side_effect=[dynamic_exc, {"acknowledged": True}, {"result": "updated"}],
        ) as http_mock:
            payload = client.upsert_balance("proj-123", "USD", costs_total=120.0, payments_total=150.0)

        self.assertEqual(payload["result"], "updated")
        self.assertEqual(http_mock.call_args_list[1].args[0], "PUT")
        self.assertEqual(http_mock.call_args_list[1].args[1], "/project-balances/_mapping")
        self.assertEqual(
            http_mock.call_args_list[1].args[2],
            {"properties": {"costs_total": {"type": "scaled_float", "scaling_factor": 100}}},
        )

    def test_search_project_payments_backfills_default_currency(self):
        client = OpenSearchClient()
        with patch.object(
            client,
            "_http_json",
            return_value={
                "hits": {
                    "hits": [
                        {"_source": {"event_id": "evt_1"}},
                        {"_source": {"event_id": "evt_2", "currency": "EUR"}},
                    ]
                }
            },
        ):
            payload = client.search_project_payments("proj-123")

        hits = payload["hits"]["hits"]
        self.assertEqual(hits[0]["_source"]["currency"], "DKK")
        self.assertEqual(hits[1]["_source"]["currency"], "EUR")

    def test_get_payment_event_backfills_default_currency(self):
        client = OpenSearchClient()
        with patch.object(client, "_http_json", return_value={"found": True, "_source": {"event_id": "evt_1"}}):
            payload = client.get_payment_event("project:proj-123", "evt_1")

        self.assertEqual(payload["_source"]["currency"], "DKK")

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
