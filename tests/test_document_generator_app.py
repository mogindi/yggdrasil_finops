import json
import os
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import document_generator_app

os.environ.setdefault("CLOUDKITTY_CURRENCY", "USD")


class DocumentGeneratorLogoRequirementTests(unittest.TestCase):
    def setUp(self):
        self._original_logo_path = os.environ.get("LOGO_PATH")
        os.environ.pop("LOGO_PATH", None)

    def tearDown(self):
        if self._original_logo_path is None:
            os.environ.pop("LOGO_PATH", None)
        else:
            os.environ["LOGO_PATH"] = self._original_logo_path

    def _request(self, method, path, body=None):
        server = ThreadingHTTPServer(("127.0.0.1", 0), document_generator_app.DocumentGeneratorHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            payload = json.dumps(body) if body is not None else None
            headers = {"Content-Type": "application/json"} if body is not None else {}
            conn.request(method, path, body=payload, headers=headers)
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8")
            conn.close()
            return resp.status, json.loads(raw)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_invoice_file_requires_logo_path(self):
        status, body = self._request("GET", "/api/projects/proj-1/invoices/inv_1/file")
        self.assertEqual(status, 500)
        self.assertIn("LOGO_PATH", body["error"])

    def test_delete_invoice_endpoint(self):
        status, created = self._request(
            "POST",
            "/api/projects/proj-1/invoices",
            body={
                "amount_due": 10.0,
                "currency": "USD",
                "customer_name": "A",
                "customer_email": "a@example.com",
            },
        )
        self.assertEqual(status, 201)

        status, deleted = self._request("DELETE", f"/api/projects/proj-1/invoices/{created['invoice_id']}")
        self.assertEqual(status, 200)
        self.assertTrue(deleted["deleted"])

        status, body = self._request("GET", f"/api/projects/proj-1/invoices/{created['invoice_id']}")
        self.assertEqual(status, 404)
        self.assertIn("does not exist", body["error"])


if __name__ == "__main__":
    unittest.main()
