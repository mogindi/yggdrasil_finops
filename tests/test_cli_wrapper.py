import json
import unittest
from unittest.mock import patch

import yggdrasil_finops


class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status = status
        self._payload = payload or {}

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class CliWrapperTests(unittest.TestCase):
    def test_project_setup_calls_payments_setup_endpoint(self):
        with patch("urllib.request.urlopen", return_value=FakeResponse(payload={"ok": True})) as mocked:
            rc = yggdrasil_finops.main([
                "project",
                "setup",
                "--project-id",
                "proj_123",
                "--api-url",
                "http://localhost:8082",
            ])
        self.assertEqual(rc, 0)
        req = mocked.call_args.args[0]
        self.assertEqual(req.method, "POST")
        self.assertEqual(req.full_url, "http://localhost:8082/api/projects/proj_123/payments/setup")

    def test_payment_create_uses_event_endpoint_with_put(self):
        with patch("urllib.request.urlopen", return_value=FakeResponse(payload={"event_id": "evt_1"})) as mocked:
            rc = yggdrasil_finops.main([
                "payment",
                "create",
                "--project-id",
                "proj_123",
                "--event-id",
                "evt_1",
                "--invoice-id",
                "inv_1",
                "--amount",
                "10.50",
                "--paid-at",
                "2026-01-01T00:00:00Z",
            ])
        self.assertEqual(rc, 0)
        req = mocked.call_args.args[0]
        self.assertEqual(req.method, "PUT")
        self.assertTrue(req.full_url.endswith("/api/projects/proj_123/payments/events/evt_1"))

    def test_invoice_show_calls_expected_endpoint(self):
        with patch("urllib.request.urlopen", return_value=FakeResponse(payload={"invoice_id": "inv_1"})) as mocked:
            rc = yggdrasil_finops.main([
                "invoice",
                "show",
                "--project-id",
                "proj_123",
                "--invoice-id",
                "inv_1",
            ])
        self.assertEqual(rc, 0)
        req = mocked.call_args.args[0]
        self.assertEqual(req.method, "GET")
        self.assertTrue(req.full_url.endswith("/api/projects/proj_123/invoices/inv_1"))


if __name__ == "__main__":
    unittest.main()
