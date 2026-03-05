import json
import os
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import checkout_app
from revolut_client import RevolutBusinessClient, RevolutError


class FakeRevolutClient:
    last_kwargs = None

    def __init__(self, debug=False):
        pass

    def create_order(self, **kwargs):
        FakeRevolutClient.last_kwargs = kwargs
        return {
            "id": "order_123",
            "state": "created",
            "public_id": "pub_order_123",
            "checkout_url": "https://sandbox-merchant.revolut.com/pay/pub_order_123",
        }


class RevolutPaymentEndpointTests(unittest.TestCase):
    def _request(self, method, path, body=None):
        server = ThreadingHTTPServer(("127.0.0.1", 0), checkout_app.CheckoutHandler)
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
        invoice = {
            "invoice_id": "inv_123",
            "amount_due": 150.0,
            "amount_paid": 0,
            "currency": "USD",
            "customer": {"email": "billing@acme.test"},
        }
        with patch.object(checkout_app.CheckoutHandler, "_fetch_invoice", return_value=invoice), patch("checkout_app.RevolutBusinessClient", FakeRevolutClient):
            status, body = self._request(
                "POST",
                "/api/projects/proj-123/payments/revolut/order",
                body={"invoice_id": invoice["invoice_id"]},
            )

        self.assertEqual(status, 201)
        self.assertEqual(body["checkout_url"], "https://sandbox-merchant.revolut.com/pay/pub_order_123")
        self.assertEqual(FakeRevolutClient.last_kwargs["currency"], "USD")
        self.assertEqual(FakeRevolutClient.last_kwargs["amount_minor"], 15000)

    def test_create_revolut_order_requires_invoice_id(self):
        with patch.object(checkout_app.CheckoutHandler, "_fetch_invoice", return_value={}):
            status, body = self._request("POST", "/api/projects/proj-123/payments/revolut/order", body={})

        self.assertEqual(status, 400)
        self.assertIn("invoice_id is required", body["error"])


class RevolutClientUnitTests(unittest.TestCase):
    def test_create_order_requires_api_key(self):
        originals = {
            "REVOLUT_API_KEY": os.environ.pop("REVOLUT_API_KEY", None),
            "REVOLUT_BUSINESS_API_URL": os.environ.get("REVOLUT_BUSINESS_API_URL"),
            "REVOLUT_ORDERS_PATH": os.environ.get("REVOLUT_ORDERS_PATH"),
            "OS_VERIFY": os.environ.get("OS_VERIFY"),
        }
        os.environ["REVOLUT_BUSINESS_API_URL"] = "https://merchant.revolut.com"
        os.environ["REVOLUT_ORDERS_PATH"] = "/api/orders"
        os.environ["OS_VERIFY"] = "1"
        try:
            client = RevolutBusinessClient()
            with self.assertRaises(RevolutError):
                client.create_order(
                    amount_minor=1000,
                    currency="USD",
                )
        finally:
            for key, value in originals.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
