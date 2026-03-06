import json
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import payments_app


class FakeOpenSearchClient:
    upsert_calls = []

    def __init__(self, debug=False):
        self.endpoint = "http://fake-opensearch:9200"

    def upsert_payment_event(self, partition, event_id, document):
        self.__class__.upsert_calls.append((partition, event_id, document))
        return {"result": "created", "_id": event_id}


class TestPaymentsAppCurrencyDefaults:
    def _request(self, method, path, body=None):
        server = ThreadingHTTPServer(("127.0.0.1", 0), payments_app.PaymentsHandler)
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

    def test_put_payment_event_defaults_currency_to_dkk(self):
        FakeOpenSearchClient.upsert_calls = []
        with patch("payments_app.OpenSearchClient", FakeOpenSearchClient):
            status, body = self._request(
                "PUT",
                "/api/projects/proj-123/payments/events/evt_1",
                body={"event_id": "evt_1", "amount": 99.95, "status": "succeeded"},
            )

        assert status == 201
        assert body["result"] == "created"
        _, _, doc = FakeOpenSearchClient.upsert_calls[0]
        assert doc["currency"] == "DKK"
