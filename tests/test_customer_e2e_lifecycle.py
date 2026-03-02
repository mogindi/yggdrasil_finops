import json
import os
import subprocess
import time
import unittest
from urllib import parse, request


class CustomerLifecycleRealE2ETests(unittest.TestCase):
    """Real infrastructure lifecycle test.

    This test intentionally uses real OpenStack, CloudKitty and OpenSearch values.
    It is opt-in and skipped unless E2E_RUN_REAL=1 is set.
    """

    @classmethod
    def setUpClass(cls):
        if os.environ.get("E2E_RUN_REAL") != "1":
            raise unittest.SkipTest("Set E2E_RUN_REAL=1 to run real lifecycle e2e test")

        cls.api_url = os.environ.get("E2E_API_URL", "http://127.0.0.1:8082").rstrip("/")
        cls.image = os.environ.get("E2E_OS_IMAGE")
        cls.flavor = os.environ.get("E2E_OS_FLAVOR")
        cls.network = os.environ.get("E2E_OS_NETWORK")
        cls.key_name = os.environ.get("E2E_OS_KEY_NAME")
        cls.security_group = os.environ.get("E2E_OS_SECURITY_GROUP")
        cls.poll_timeout = int(os.environ.get("E2E_COST_POLL_TIMEOUT_SECONDS", "600"))
        cls.poll_interval = int(os.environ.get("E2E_COST_POLL_INTERVAL_SECONDS", "20"))

        missing = [
            name
            for name, value in {
                "E2E_OS_IMAGE": cls.image,
                "E2E_OS_FLAVOR": cls.flavor,
                "E2E_OS_NETWORK": cls.network,
            }.items()
            if not value
        ]
        if missing:
            raise unittest.SkipTest(f"Missing required vars: {', '.join(missing)}")

    def setUp(self):
        suffix = str(int(time.time()))[-8:]
        self.project_name = f"e2e-finops-{suffix}"
        self.project_id = ""
        self.server_name = f"e2e-vm-{suffix}"
        self.server_id = ""

    def tearDown(self):
        if self.server_id:
            self._run_openstack(["server", "delete", self.server_id], check=False)
        if self.project_id:
            self._cleanup_opensearch_partition(self.project_id)
            self._run_openstack(["project", "delete", self.project_id], check=False)

    def _run_openstack(self, args, check=True):
        cmd = ["openstack", *args, "-f", "json"]
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
        if check and proc.returncode != 0:
            self.fail(f"Command failed: {' '.join(cmd)}\nstdout={proc.stdout}\nstderr={proc.stderr}")
        return proc

    def _api_json(self, method, path, payload=None):
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = request.Request(f"{self.api_url}{path}", method=method, data=data, headers=headers)
        with request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    @staticmethod
    def _payments_index_name(project_id: str) -> str:
        raw = project_id.strip().lower()
        normalized = "".join(ch if (ch.isalnum() or ch in "_-") else "-" for ch in raw).strip("-") or "default"
        return f"payments-project-{normalized}"

    def _cleanup_opensearch_partition(self, project_id: str):
        endpoint = os.environ.get("OPENSEARCH_URL", "http://localhost:9200").rstrip("/")
        verify = os.environ.get("OS_VERIFY", "true").lower() not in {"0", "false", "no"}
        ctx = None
        if not verify and endpoint.startswith("https"):
            import ssl

            ctx = ssl._create_unverified_context()

        index_name = self._payments_index_name(project_id)
        for method, path in [
            ("DELETE", f"/{index_name}"),
            ("DELETE", f"/project-balances/_doc/{parse.quote(project_id)}"),
        ]:
            req = request.Request(f"{endpoint}{path}", method=method)
            try:
                with request.urlopen(req, timeout=20, context=ctx):
                    pass
            except Exception:
                pass

    def test_real_customer_lifecycle_with_vm_usage_rating_payment_and_graph(self):
        project = json.loads(self._run_openstack(["project", "create", self.project_name]).stdout)
        self.project_id = project["id"]

        status, setup_payload = self._api_json("POST", f"/api/projects/{self.project_id}/payments/setup")
        self.assertEqual(status, 201)
        self.assertIn("template", setup_payload)

        server_cmd = [
            "server",
            "create",
            self.server_name,
            "--image",
            self.image,
            "--flavor",
            self.flavor,
            "--network",
            self.network,
            "--wait",
        ]
        if self.key_name:
            server_cmd.extend(["--key-name", self.key_name])
        if self.security_group:
            server_cmd.extend(["--security-group", self.security_group])

        server = json.loads(self._run_openstack(server_cmd).stdout)
        self.server_id = server["id"]

        started = time.time()
        aggregate = 0.0
        while time.time() - started < self.poll_timeout:
            status, costs_payload = self._api_json(
                "GET", f"/api/projects/{self.project_id}/costs?resolution=minute&include_series=true"
            )
            self.assertEqual(status, 200)
            aggregate = float(costs_payload.get("aggregate_cost_now", 0.0))
            if aggregate > 0:
                break
            time.sleep(self.poll_interval)

        self.assertGreater(aggregate, 0.0, "Expected rated usage cost to become > 0 after creating VM")

        status, monthly_payload = self._api_json("GET", f"/api/projects/{self.project_id}/costs/monthly")
        self.assertEqual(status, 200)
        self.assertEqual(monthly_payload["resolution"], "month")

        invoice_amount = round(aggregate, 2)
        status, invoice = self._api_json(
            "POST",
            f"/api/projects/{self.project_id}/invoices",
            {
                "amount_due": invoice_amount,
                "currency": monthly_payload["currency"],
                "customer_name": "Real Customer",
                "customer_email": "billing@example.com",
                "description": "Real rated VM usage",
            },
        )
        self.assertEqual(status, 201)

        event_id = f"evt-{int(time.time())}"
        status, payment = self._api_json(
            "PUT",
            f"/api/projects/{self.project_id}/payments/events/{event_id}",
            {
                "invoice_id": invoice["invoice_id"],
                "amount": invoice_amount,
                "currency": invoice["currency"],
                "status": "succeeded",
                "direction": "in",
                "provider": "manual",
                "method": "bank_transfer",
            },
        )
        self.assertEqual(status, 201)
        self.assertIn("result", payment)

        status, receipt = self._api_json(
            "POST",
            f"/api/projects/{self.project_id}/receipts",
            {
                "invoice_id": invoice["invoice_id"],
                "amount_paid": invoice_amount,
                "currency": invoice["currency"],
                "payment_method": "bank_transfer",
                "payment_reference": f"e2e-{event_id}",
            },
        )
        self.assertEqual(status, 201)
        self.assertIn("receipt_id", receipt)

        req = request.Request(f"{self.api_url}/api/projects/{self.project_id}/costs/monthly/graph", method="GET")
        with request.urlopen(req, timeout=60) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
            self.assertEqual(resp.status, 200)
            self.assertIn("<svg", html)
            self.assertIn("Monthly Cost History", html)


if __name__ == "__main__":
    unittest.main()
