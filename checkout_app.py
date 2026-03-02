#!/usr/bin/env python3
import argparse
import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import error, request
from urllib.parse import urlparse

from revolut_client import RevolutApiError, RevolutBusinessClient, RevolutError

DOCUMENT_GENERATOR_SERVICE_URL = os.environ.get("DOCUMENT_GENERATOR_SERVICE_URL", "http://document_generator:8080")


class CheckoutHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            return self._json({"status": "ok", "service": "checkout"})
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self):
        parts = urlparse(self.path).path.split("/")
        if len(parts) == 7 and parts[1] == "api" and parts[2] == "projects" and parts[4] == "payments" and parts[5] == "revolut" and parts[6] == "order":
            return self._project_payments_revolut_create(parts[3])
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length).decode("utf-8")) if length > 0 else {}

    def _fetch_invoice(self, project_id: str, invoice_id: str) -> dict:
        req = request.Request(f"{DOCUMENT_GENERATOR_SERVICE_URL}/api/projects/{project_id}/invoices/{invoice_id}", headers={"Accept": "application/json"})
        with request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _project_payments_revolut_create(self, project_id: str):
        body = self._read_json_body()
        invoice_id = body.get("invoice_id", "")
        if not invoice_id:
            return self._json({"error": "invoice_id is required"}, status=400)
        try:
            invoice = self._fetch_invoice(project_id, invoice_id)
        except error.HTTPError as exc:
            data = exc.read().decode("utf-8")
            try:
                payload = json.loads(data)
            except Exception:
                payload = {"error": data}
            return self._json(payload, status=exc.code)
        except error.URLError as exc:
            return self._json({"error": f"document service unavailable: {exc.reason}"}, status=502)

        remaining_amount = float(invoice.get("amount_due", 0)) - float(invoice.get("amount_paid", 0))
        if remaining_amount <= 0:
            return self._json({"error": f"Invoice '{invoice_id}' is already fully paid"}, status=400)

        client = RevolutBusinessClient()
        try:
            response = client.create_order(
                order_id=invoice_id,
                amount=float(body.get("amount", remaining_amount)),
                currency=body.get("currency", invoice.get("currency", "USD")),
                description=body.get("description", invoice.get("description", "Project invoice payment")),
                customer_email=invoice.get("customer", {}).get("email", ""),
                success_url=body.get("success_url"),
                metadata={"project_id": project_id, "invoice_id": invoice_id},
            )
            return self._json(response, status=201)
        except RevolutApiError as exc:
            return self._json({"error": str(exc), "details": exc.body}, status=502)
        except RevolutError as exc:
            return self._json({"error": str(exc)}, status=502)

    def _json(self, payload: dict, status: int = 200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run() -> None:
    parser = argparse.ArgumentParser(description="Checkout service")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    args = parser.parse_args()
    ThreadingHTTPServer(("0.0.0.0", args.port), CheckoutHandler).serve_forever()


if __name__ == "__main__":
    run()
