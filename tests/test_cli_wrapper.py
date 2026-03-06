import json
import os
import io
import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path
import unittest
from unittest.mock import patch


def _load_cli_module():
    cli_path = Path(__file__).resolve().parents[1] / "yggdrasil_finops"
    loader = SourceFileLoader("yggdrasil_finops", str(cli_path))
    spec = importlib.util.spec_from_loader("yggdrasil_finops", loader)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


yggdrasil_finops = _load_cli_module()
os.environ.setdefault("CLOUDKITTY_CURRENCY", "USD")



class FakeResponse:
    def __init__(self, status=200, payload=None, raw_body=None, headers=None):
        self.status = status
        self._payload = payload or {}
        self._raw_body = raw_body
        self.headers = headers or {"Content-Type": "application/json"}

    def read(self):
        if self._raw_body is not None:
            return self._raw_body
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class CliWrapperTests(unittest.TestCase):
    def test_root_help_lists_only_command_paths(self):
        with patch("sys.stdout", new=io.StringIO()) as output:
            rc = yggdrasil_finops.main(["--help"])

        self.assertEqual(rc, 0)
        text = output.getvalue()
        self.assertIn("Available commands:", text)
        self.assertIn("yggdrasil_finops receipt create", text)
        self.assertIn("yggdrasil_finops receipt list", text)
        self.assertNotIn("--project-id", text)

    def test_subcommand_help_still_shows_options(self):
        with patch("sys.stdout", new=io.StringIO()) as output:
            with self.assertRaises(SystemExit) as exc:
                yggdrasil_finops.main(["receipt", "create", "-h"])

        self.assertEqual(exc.exception.code, 0)
        text = output.getvalue()
        self.assertIn("--project-id PROJECT_ID", text)
        self.assertIn("--invoice-id INVOICE_ID", text)
        self.assertIn("ISO8601 datetime, e.g. 2026-01-01T00:00:00Z", text)

    def test_help_includes_option_format_examples(self):
        with patch("sys.stdout", new=io.StringIO()) as output:
            with self.assertRaises(SystemExit) as exc:
                yggdrasil_finops.main(["cost", "month", "-h"])

        self.assertEqual(exc.exception.code, 0)
        text = output.getvalue()
        self.assertIn("--month YYYY-MM", text)
        self.assertIn("Month in YYYY-MM format, e.g. 2026-01", text)

    def test_cost_monthly_calls_expected_endpoint(self):
        with patch("urllib.request.urlopen", return_value=FakeResponse(payload={"series": []})) as mocked:
            rc = yggdrasil_finops.main([
                "cost",
                "monthly",
                "--project-id",
                "proj_123",
                "--api-url",
                "http://localhost:8082",
            ])
        self.assertEqual(rc, 0)
        req = mocked.call_args.args[0]
        self.assertEqual(req.method, "GET")
        self.assertEqual(req.full_url, "http://localhost:8082/api/projects/proj_123/costs/monthly")

    def test_cost_monthly_graph_returns_html(self):
        with patch(
            "urllib.request.urlopen",
            return_value=FakeResponse(raw_body=b"<html><body>graph</body></html>", headers={"Content-Type": "text/html"}),
        ) as mocked:
            rc = yggdrasil_finops.main([
                "cost",
                "monthly-graph",
                "--project-id",
                "proj_123",
            ])
        self.assertEqual(rc, 0)
        req = mocked.call_args.args[0]
        self.assertEqual(req.method, "GET")
        self.assertTrue(req.full_url.endswith("/api/projects/proj_123/costs/monthly/graph"))

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
        payload = json.loads(req.data.decode("utf-8"))
        self.assertEqual(payload["direction"], "inbound")
        self.assertNotIn("payment_direction", payload)

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


    def test_payment_balance_get_calls_expected_endpoint(self):
        with patch("urllib.request.urlopen", return_value=FakeResponse(payload={"found": True})) as mocked:
            rc = yggdrasil_finops.main([
                "payment",
                "balance",
                "--project-id",
                "proj_123",
            ])
        self.assertEqual(rc, 0)
        req = mocked.call_args.args[0]
        self.assertEqual(req.method, "GET")
        self.assertTrue(req.full_url.endswith("/api/projects/proj_123/payments/balance"))


    def test_payment_balance_get_with_as_of_date_adds_query_string(self):
        with patch("urllib.request.urlopen", return_value=FakeResponse(payload={"found": True})) as mocked:
            rc = yggdrasil_finops.main([
                "payment",
                "balance",
                "--project-id",
                "proj_123",
                "--as-of-date",
                "2026-02-01T00:00:00Z",
            ])
        self.assertEqual(rc, 0)
        req = mocked.call_args.args[0]
        self.assertEqual(req.method, "GET")
        self.assertTrue(req.full_url.endswith("/api/projects/proj_123/payments/balance?as_of_date=2026-02-01T00%3A00%3A00Z"))

    def test_payment_balance_get_with_window_dates_adds_all_query_params(self):
        with patch("urllib.request.urlopen", return_value=FakeResponse(payload={"found": True})) as mocked:
            rc = yggdrasil_finops.main([
                "payment",
                "balance",
                "--project-id",
                "proj_123",
                "--costs-from-date",
                "2026-01-01T00:00:00Z",
                "--costs-to-date",
                "2026-01-31T23:59:59Z",
                "--payments-from-date",
                "2026-01-01T00:00:00Z",
                "--payments-to-date",
                "2026-02-05T10:00:00Z",
            ])
        self.assertEqual(rc, 0)
        req = mocked.call_args.args[0]
        self.assertEqual(req.method, "GET")
        self.assertIn("costs_from_date=2026-01-01T00%3A00%3A00Z", req.full_url)
        self.assertIn("costs_to_date=2026-01-31T23%3A59%3A59Z", req.full_url)
        self.assertIn("payments_from_date=2026-01-01T00%3A00%3A00Z", req.full_url)
        self.assertIn("payments_to_date=2026-02-05T10%3A00%3A00Z", req.full_url)

    def test_payment_set_balance_puts_costs_and_payments_totals(self):
        with patch("urllib.request.urlopen", return_value=FakeResponse(payload={"result": "updated"})) as mocked:
            rc = yggdrasil_finops.main([
                "payment",
                "set-balance",
                "--project-id",
                "proj_123",
                "--currency",
                "USD",
                "--costs-total",
                "120.0",
                "--payments-total",
                "150.0",
            ])
        self.assertEqual(rc, 0)
        req = mocked.call_args.args[0]
        self.assertEqual(req.method, "PUT")
        self.assertTrue(req.full_url.endswith("/api/projects/proj_123/payments/balance"))
        payload = json.loads(req.data.decode("utf-8"))
        self.assertEqual(payload["currency"], "USD")
        self.assertEqual(payload["costs_total"], 120.0)
        self.assertEqual(payload["payments_total"], 150.0)


    def test_invoice_file_download_uses_file_endpoint(self):
        with patch("urllib.request.urlopen", return_value=FakeResponse(raw_body=b"%PDF-1.4 mock", headers={"Content-Type": "application/pdf"})) as mocked:
            with patch("builtins.open", unittest.mock.mock_open()) as open_mock:
                rc = yggdrasil_finops.main([
                    "invoice",
                    "file",
                    "--project-id",
                    "proj_123",
                    "--invoice-id",
                    "inv_1",
                    "--download-path",
                    "./inv_1.pdf",
                ])
        self.assertEqual(rc, 0)
        req = mocked.call_args.args[0]
        self.assertEqual(req.method, "GET")
        self.assertIn("/api/projects/proj_123/invoices/inv_1/file", req.full_url)
        open_mock.assert_called_once_with("./inv_1.pdf", "wb")

    def test_invoice_file_without_download_path_is_not_written(self):
        with patch("urllib.request.urlopen", return_value=FakeResponse(raw_body=b"%PDF-1.4 mock", headers={"Content-Type": "application/pdf"})), patch("sys.stdout", new_callable=io.StringIO) as stdout:
            rc = yggdrasil_finops.main([
                "invoice",
                "file",
                "--project-id",
                "proj_123",
                "--invoice-id",
                "inv_1",
            ])

        self.assertEqual(rc, 0)
        body = json.loads(stdout.getvalue())
        self.assertIsNone(body["saved_to"])
        self.assertIn("--download-path", body["note"])


if __name__ == "__main__":
    unittest.main()
