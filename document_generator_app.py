#!/usr/bin/env python3
import argparse
import html
import json
import logging
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from billing_service import (
    BillingError,
    BillingService,
    InMemoryBillingRepository,
    InvoiceCreateRequest,
    InvoiceNotFoundError,
    OpenSearchBillingRepository,
    ReceiptCreateRequest,
    ReceiptNotFoundError,
)
from brevo_client import BrevoClient, BrevoError
from document_service import DocumentError, DocumentService
from currency import get_default_currency
from opensearch_client import OpenSearchApiError, OpenSearchClient, OpenSearchError
from startup_validation import describe_env, env_flag_enabled, print_env_resolution, validate_http_endpoint


DEBUG_MODE = env_flag_enabled("DEBUG", default=False)
LOGGER = logging.getLogger("document_generator")


def _build_billing_service() -> BillingService:
    opensearch_url = os.environ.get("OPENSEARCH_URL", "").strip()
    if opensearch_url:
        return BillingService(OpenSearchBillingRepository(OpenSearchClient(debug=DEBUG_MODE)))
    return BillingService(InMemoryBillingRepository())


BILLING_SERVICE = _build_billing_service()
DOCUMENT_SERVICE = DocumentService()


class DocumentGeneratorHandler(BaseHTTPRequestHandler):
    def _log_opensearch_failure(self, exc: OpenSearchApiError | OpenSearchError, status: int) -> None:
        if isinstance(exc, OpenSearchApiError):
            body_preview = (exc.body or "")[:1000]
            LOGGER.error(
                "OpenSearch request failed while handling method=%s path=%s status=%s opensearch_status=%s opensearch_url=%s response_body=%s",
                self.command,
                self.path,
                status,
                exc.status_code,
                exc.url,
                body_preview,
            )
            return
        LOGGER.error(
            "OpenSearch connection failed while handling method=%s path=%s status=%s error=%s",
            self.command,
            self.path,
            status,
            str(exc),
        )

    def _log_api_request(self):
        if self.path.startswith("/api/") or self.path == "/healthz":
            LOGGER.debug(
                "API call: method=%s path=%s content_length=%s",
                self.command,
                self.path,
                self.headers.get("Content-Length", "0"),
            )

    def do_GET(self):
        self._log_api_request()
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            return self._json({"status": "ok", "service": "document_generator"})
        parts = parsed.path.split("/")
        if len(parts) >= 5 and parts[1] == "api" and parts[2] == "customers":
            customer_id = parts[3]
            if parts[4] == "invoices":
                return self._project_invoices_get(customer_id, parts)
            if parts[4] == "receipts":
                return self._project_receipts_get(customer_id, parts)
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self):
        self._log_api_request()
        parts = urlparse(self.path).path.split("/")
        if len(parts) >= 5 and parts[1] == "api" and parts[2] == "customers":
            customer_id = parts[3]
            if parts[4] == "invoices":
                return self._project_invoices_post(customer_id)
            if parts[4] == "receipts":
                return self._project_receipts_post(customer_id)
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_DELETE(self):
        self._log_api_request()
        parts = urlparse(self.path).path.split("/")
        if len(parts) == 6 and parts[1] == "api" and parts[2] == "customers" and parts[4] == "invoices":
            return self._project_invoices_delete(parts[3], parts[5])
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length).decode("utf-8")) if length > 0 else {}

    def _require_logo_path(self) -> str | None:
        logo_path = os.environ.get("LOGO_PATH", "").strip()
        if not logo_path:
            self._json({"error": "LOGO_PATH environment variable is required"}, status=500)
            return None
        return logo_path

    def _project_invoices_get(self, customer_id: str, parts: list[str]):
        try:
            if len(parts) == 5:
                return self._json({"invoices": BILLING_SERVICE.list_invoices(customer_id)})
            if len(parts) == 6:
                return self._json(BILLING_SERVICE.get_invoice(customer_id, parts[5]))
            if len(parts) == 7 and parts[6] == "file":
                return self._project_invoice_file_get(customer_id, parts[5])
        except InvoiceNotFoundError as exc:
            return self._json({"error": str(exc)}, status=404)
        except BillingError as exc:
            return self._json({"error": str(exc)}, status=400)
        except (OpenSearchApiError, OpenSearchError) as exc:
            self._log_opensearch_failure(exc, status=502)
            return self._json({"error": str(exc)}, status=502)
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def _project_invoices_post(self, customer_id: str):
        body = self._read_json_body()
        req = InvoiceCreateRequest(float(body.get("amount_due", 0)), body.get("currency", get_default_currency()), body.get("customer_name", ""), body.get("customer_email", ""), body.get("due_at"), body.get("description", ""))
        try:
            return self._json(BILLING_SERVICE.create_invoice(customer_id, req), status=201)
        except (OpenSearchApiError, OpenSearchError) as exc:
            self._log_opensearch_failure(exc, status=502)
            return self._json({"error": str(exc)}, status=502)

    def _project_invoices_delete(self, customer_id: str, invoice_id: str):
        try:
            BILLING_SERVICE.delete_invoice(customer_id, invoice_id)
            return self._json({"deleted": True, "invoice_id": invoice_id})
        except InvoiceNotFoundError as exc:
            return self._json({"error": str(exc)}, status=404)
        except (BillingError, OpenSearchApiError, OpenSearchError) as exc:
            if isinstance(exc, (OpenSearchApiError, OpenSearchError)):
                self._log_opensearch_failure(exc, status=400)
            return self._json({"error": str(exc)}, status=400)

    def _project_receipts_get(self, customer_id: str, parts: list[str]):
        try:
            if len(parts) == 7 and parts[6] == "file":
                return self._project_receipt_file_get(customer_id, parts[5])
            return self._json({"receipts": BILLING_SERVICE.list_receipts(customer_id)})
        except ReceiptNotFoundError as exc:
            return self._json({"error": str(exc)}, status=404)

    def _project_receipts_post(self, customer_id: str):
        body = self._read_json_body()
        req = ReceiptCreateRequest(body.get("invoice_id", ""), float(body.get("amount_paid", 0)), body.get("currency", get_default_currency()), body.get("paid_at"), body.get("payment_method", "unknown"), body.get("payment_reference", ""))
        try:
            return self._json(BILLING_SERVICE.create_receipt(customer_id, req), status=201)
        except (InvoiceNotFoundError, BillingError) as exc:
            return self._json({"error": str(exc)}, status=400)

    def _project_invoice_file_get(self, customer_id: str, invoice_id: str):
        q = parse_qs(urlparse(self.path).query)
        logo_path = self._require_logo_path()
        if not logo_path:
            return
        invoice = BILLING_SERVICE.get_invoice(customer_id, invoice_id)
        try:
            pdf = DOCUMENT_SERVICE.build_invoice_pdf(invoice, logo_path=logo_path)
        except DocumentError as exc:
            return self._json({"error": str(exc)}, status=400)
        if q.get("send_email", ["false"])[0].lower() == "true":
            to_email = q.get("email", [None])[0] or invoice.get("customer", {}).get("email", "")
            try:
                BrevoClient(debug=DEBUG_MODE).send_pdf(to_email=to_email, subject=f"Invoice {invoice_id}", html_content=f"<p>Invoice <b>{html.escape(invoice_id)}</b></p>", filename=f"{invoice_id}.pdf", content=pdf)
            except BrevoError as exc:
                return self._json({"error": str(exc)}, status=502)
        if q.get("view", ["pdf"])[0] == "html":
            return self._html(DOCUMENT_SERVICE.build_pdf_html_page(f"Invoice {invoice_id}", f"{invoice_id}.pdf", pdf))
        return self._pdf(pdf, f"{invoice_id}.pdf", q.get("download", ["false"])[0].lower() == "true")

    def _project_receipt_file_get(self, customer_id: str, receipt_id: str):
        q = parse_qs(urlparse(self.path).query)
        logo_path = self._require_logo_path()
        if not logo_path:
            return
        receipt = BILLING_SERVICE.get_receipt(customer_id, receipt_id)
        invoice = BILLING_SERVICE.get_invoice(customer_id, receipt.get("invoice_id", ""))
        try:
            pdf = DOCUMENT_SERVICE.build_receipt_pdf(receipt, invoice, logo_path=logo_path)
        except DocumentError as exc:
            return self._json({"error": str(exc)}, status=400)
        if q.get("view", ["pdf"])[0] == "html":
            return self._html(DOCUMENT_SERVICE.build_pdf_html_page(f"Receipt {receipt_id}", f"{receipt_id}.pdf", pdf))
        return self._pdf(pdf, f"{receipt_id}.pdf", q.get("download", ["false"])[0].lower() == "true")

    def _json(self, payload: dict, status: int = 200):
        body = json.dumps(payload).encode()
        if self.path.startswith("/api/") or self.path == "/healthz":
            LOGGER.debug(
                "API response: method=%s path=%s status=%s payload_bytes=%s",
                self.command,
                self.path,
                status,
                len(body),
            )
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, payload: str):
        body = payload.encode()
        if self.path.startswith("/api/"):
            LOGGER.debug(
                "API response: method=%s path=%s status=%s content_type=text/html payload_bytes=%s",
                self.command,
                self.path,
                200,
                len(body),
            )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _pdf(self, payload: bytes, filename: str, download: bool):
        if self.path.startswith("/api/"):
            LOGGER.debug(
                "API response: method=%s path=%s status=%s content_type=application/pdf filename=%s payload_bytes=%s",
                self.command,
                self.path,
                200,
                filename,
                len(payload),
            )
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Disposition", f'{"attachment" if download else "inline"}; filename="{filename}"')
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def run() -> None:
    parser = argparse.ArgumentParser(description="Document generator service")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()
    global DEBUG_MODE
    DEBUG_MODE = args.debug or env_flag_enabled("DEBUG", default=False)
    if DEBUG_MODE:
        logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    global BILLING_SERVICE
    BILLING_SERVICE = _build_billing_service()

    opensearch_url = os.environ.get("OPENSEARCH_URL", "").strip()
    if opensearch_url:
        value, using_default = describe_env("OPENSEARCH_URL")
        print_env_resolution("OPENSEARCH_URL", value, using_default)
        validate_http_endpoint("OPENSEARCH_URL", value, health_path="/")

        os_verify, os_verify_defaulted = describe_env("OS_VERIFY")
        print_env_resolution("OS_VERIFY", os_verify, os_verify_defaulted)

    ThreadingHTTPServer(("0.0.0.0", args.port), DocumentGeneratorHandler).serve_forever()


if __name__ == "__main__":
    run()
