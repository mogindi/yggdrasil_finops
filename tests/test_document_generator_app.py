import json
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import document_generator_app


class DocumentGeneratorLogoRequirementTests(unittest.TestCase):
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
        self.assertEqual(status, 400)
        self.assertIn("logo_path", body["error"])


if __name__ == "__main__":
    unittest.main()
