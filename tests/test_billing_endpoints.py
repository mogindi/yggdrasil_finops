import json
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import app
from billing_service import BillingService, InMemoryBillingRepository


class FakeCloudKittyClient:
    def __init__(self, debug=False):
        pass

    def ensure_project_exists(self, project_id):
        return None


class BillingEndpointsTests(unittest.TestCase):
    def _request(self, method, path, body=None, expect_json=True):
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.CostHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            payload = json.dumps(body) if body is not None else None
            headers = {"Content-Type": "application/json"} if body is not None else {}
            conn.request(method, path, body=payload, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            content_type = resp.getheader("Content-Type") or ""
            conn.close()
            if expect_json:
                data = json.loads(raw.decode("utf-8"))
                return resp.status, data
            return resp.status, raw, content_type
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_create_invoice_and_receipt_flow(self):
        app.BILLING_SERVICE = BillingService(InMemoryBillingRepository())
        with patch("app.CloudKittyClient", FakeCloudKittyClient):
            status, invoice = self._request(
                "POST",
                "/api/projects/proj-123/invoices",
                body={
                    "amount_due": 250.0,
                    "currency": "USD",
                    "customer_name": "Acme Corp",
                    "customer_email": "billing@acme.test",
                    "description": "January cloud usage",
                },
            )
            self.assertEqual(status, 201)
            self.assertEqual(invoice["status"], "open")
            self.assertEqual(invoice["amount_paid"], 0.0)

            status, receipt = self._request(
                "POST",
                "/api/projects/proj-123/receipts",
                body={
                    "invoice_id": invoice["invoice_id"],
                    "amount_paid": 250.0,
                    "currency": "USD",
                    "payment_method": "wire_transfer",
                    "payment_reference": "wire-001",
                },
            )
            self.assertEqual(status, 201)
            self.assertEqual(receipt["invoice_id"], invoice["invoice_id"])

            status, invoice_after = self._request("GET", f"/api/projects/proj-123/invoices/{invoice['invoice_id']}")
            self.assertEqual(status, 200)
            self.assertEqual(invoice_after["status"], "paid")
            self.assertEqual(invoice_after["amount_paid"], 250.0)

    def test_receipt_creation_requires_existing_invoice(self):
        app.BILLING_SERVICE = BillingService(InMemoryBillingRepository())
        with patch("app.CloudKittyClient", FakeCloudKittyClient):
            status, body = self._request(
                "POST",
                "/api/projects/proj-123/receipts",
                body={"invoice_id": "inv_missing", "amount_paid": 10.0, "currency": "USD"},
            )

        self.assertEqual(status, 404)
        self.assertIn("does not exist", body["error"])

    def test_invoice_file_endpoint_returns_pdf_and_html(self):
        app.BILLING_SERVICE = BillingService(InMemoryBillingRepository())
        with patch("app.CloudKittyClient", FakeCloudKittyClient):
            status, invoice = self._request(
                "POST",
                "/api/projects/proj-123/invoices",
                body={
                    "amount_due": 75.0,
                    "currency": "USD",
                    "customer_name": "Acme Corp",
                    "customer_email": "billing@acme.test",
                },
            )
            self.assertEqual(status, 201)

            status, pdf_body, content_type = self._request(
                "GET",
                f"/api/projects/proj-123/invoices/{invoice['invoice_id']}/file",
                expect_json=False,
            )
            self.assertEqual(status, 200)
            self.assertEqual(content_type, "application/pdf")
            self.assertTrue(pdf_body.startswith(b"%PDF"))

            status, html_body, content_type = self._request(
                "GET",
                f"/api/projects/proj-123/invoices/{invoice['invoice_id']}/file?view=html",
                expect_json=False,
            )
            self.assertEqual(status, 200)
            self.assertTrue(content_type.startswith("text/html"))
            self.assertIn(b"iframe", html_body)


if __name__ == "__main__":
    unittest.main()
