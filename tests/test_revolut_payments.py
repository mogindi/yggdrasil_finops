import json
import os
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import app
from billing_service import BillingService, InMemoryBillingRepository, InvoiceCreateRequest
from revolut_client import RevolutBusinessClient, RevolutError


class FakeCloudKittyClient:
    def __init__(self, debug=False):
        pass

    def ensure_project_exists(self, project_id):
        return None


class FakeRevolutClient:
    def create_order(self, **kwargs):
        return {
            "id": "order_123",
            "state": "created",
            "public_id": "pub_order_123",
            "checkout_url": "https://sandbox-merchant.revolut.com/pay/pub_order_123",
            "submitted": kwargs,
        }


class RevolutPaymentEndpointTests(unittest.TestCase):
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

    def test_create_revolut_order_for_invoice(self):
        app.BILLING_SERVICE = BillingService(InMemoryBillingRepository())
        invoice = app.BILLING_SERVICE.create_invoice(
            "proj-123",
            InvoiceCreateRequest(
                amount_due=150.0,
                currency="USD",
                customer_name="Acme Corp",
                customer_email="billing@acme.test",
                due_at=None,
                description="Monthly cloud bill",
            ),
        )
        with patch("app.CloudKittyClient", FakeCloudKittyClient), patch("app.RevolutBusinessClient", FakeRevolutClient):
            status, body = self._request(
                "POST",
                "/api/projects/proj-123/payments/revolut/order",
                body={"invoice_id": invoice["invoice_id"], "success_url": "https://example.com/success"},
            )

        self.assertEqual(status, 201)
        self.assertEqual(body["state"], "created")
        self.assertEqual(body["submitted"]["order_id"], invoice["invoice_id"])
        self.assertEqual(body["submitted"]["amount"], 150.0)

    def test_create_revolut_order_requires_invoice_id(self):
        app.BILLING_SERVICE = BillingService(InMemoryBillingRepository())
        with patch("app.CloudKittyClient", FakeCloudKittyClient):
            status, body = self._request("POST", "/api/projects/proj-123/payments/revolut/order", body={})

        self.assertEqual(status, 400)
        self.assertIn("invoice_id is required", body["error"])


class RevolutClientUnitTests(unittest.TestCase):
    def test_create_order_requires_api_key(self):
        original = os.environ.pop("REVOLUT_API_KEY", None)
        try:
            client = RevolutBusinessClient()
            with self.assertRaises(RevolutError):
                client.create_order(
                    order_id="inv_001",
                    amount=10.0,
                    currency="USD",
                    description="desc",
                    customer_email="billing@example.com",
                    success_url=None,
                )
        finally:
            if original is not None:
                os.environ["REVOLUT_API_KEY"] = original


if __name__ == "__main__":
    unittest.main()
