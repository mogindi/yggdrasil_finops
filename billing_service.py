import datetime as dt
import threading
import uuid
from dataclasses import dataclass
from urllib import parse

from opensearch_client import OpenSearchApiError, OpenSearchClient


class BillingError(Exception):
    pass


class InvoiceNotFoundError(BillingError):
    pass


class ReceiptNotFoundError(BillingError):
    pass


@dataclass
class InvoiceCreateRequest:
    amount_due: float
    currency: str
    customer_name: str
    customer_email: str
    due_at: str | None = None
    description: str = ""


@dataclass
class ReceiptCreateRequest:
    invoice_id: str
    amount_paid: float
    currency: str
    paid_at: str | None = None
    payment_method: str = "unknown"
    payment_reference: str = ""


class InMemoryBillingRepository:
    """Simple storage adapter to keep billing flows isolated from HTTP handlers."""

    def __init__(self):
        self._lock = threading.Lock()
        self._invoices: dict[str, dict[str, dict]] = {}
        self._receipts: dict[str, dict[str, dict]] = {}

    def create_invoice(self, customer_id: str, invoice: dict) -> dict:
        with self._lock:
            self._invoices.setdefault(customer_id, {})[invoice["invoice_id"]] = invoice
        return invoice

    def list_invoices(self, customer_id: str) -> list[dict]:
        with self._lock:
            return list(self._invoices.get(customer_id, {}).values())

    def get_invoice(self, customer_id: str, invoice_id: str) -> dict | None:
        with self._lock:
            return self._invoices.get(customer_id, {}).get(invoice_id)

    def save_invoice(self, customer_id: str, invoice: dict) -> dict:
        with self._lock:
            self._invoices.setdefault(customer_id, {})[invoice["invoice_id"]] = invoice
        return invoice

    def delete_invoice(self, customer_id: str, invoice_id: str) -> bool:
        with self._lock:
            project_invoices = self._invoices.get(customer_id, {})
            return project_invoices.pop(invoice_id, None) is not None

    def create_receipt(self, customer_id: str, receipt: dict) -> dict:
        with self._lock:
            self._receipts.setdefault(customer_id, {})[receipt["receipt_id"]] = receipt
        return receipt

    def get_receipt(self, customer_id: str, receipt_id: str) -> dict | None:
        with self._lock:
            return self._receipts.get(customer_id, {}).get(receipt_id)

    def list_receipts(self, customer_id: str) -> list[dict]:
        with self._lock:
            return list(self._receipts.get(customer_id, {}).values())


class BillingService:
    def __init__(self, repository: InMemoryBillingRepository):
        self._repo = repository

    @staticmethod
    def _utc_now_iso() -> str:
        return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()

    def create_invoice(self, customer_id: str, request: InvoiceCreateRequest) -> dict:
        created_at = self._utc_now_iso()
        invoice = {
            "invoice_id": f"inv_{uuid.uuid4().hex[:16]}",
            "customer_id": customer_id,
            "customer": {
                "name": request.customer_name,
                "email": request.customer_email,
            },
            "description": request.description,
            "amount_due": float(request.amount_due),
            "amount_paid": 0.0,
            "currency": request.currency,
            "status": "open",
            "created_at": created_at,
            "due_at": request.due_at,
            "updated_at": created_at,
        }
        return self._repo.create_invoice(customer_id, invoice)

    def list_invoices(self, customer_id: str) -> list[dict]:
        return self._repo.list_invoices(customer_id)

    def get_invoice(self, customer_id: str, invoice_id: str) -> dict:
        invoice = self._repo.get_invoice(customer_id, invoice_id)
        if not invoice:
            raise InvoiceNotFoundError(f"Invoice '{invoice_id}' does not exist for customer '{customer_id}'")
        return invoice

    def delete_invoice(self, customer_id: str, invoice_id: str) -> None:
        deleted = self._repo.delete_invoice(customer_id, invoice_id)
        if not deleted:
            raise InvoiceNotFoundError(f"Invoice '{invoice_id}' does not exist for customer '{customer_id}'")

    def create_receipt(self, customer_id: str, request: ReceiptCreateRequest) -> dict:
        invoice = self.get_invoice(customer_id, request.invoice_id)
        invoice["amount_paid"] = float(invoice.get("amount_paid", 0.0)) + float(request.amount_paid)
        if invoice["amount_paid"] >= float(invoice["amount_due"]):
            invoice["status"] = "paid"
        else:
            invoice["status"] = "partially_paid"
        invoice["updated_at"] = self._utc_now_iso()
        self._repo.save_invoice(customer_id, invoice)

        receipt = {
            "receipt_id": f"rcpt_{uuid.uuid4().hex[:16]}",
            "customer_id": customer_id,
            "invoice_id": request.invoice_id,
            "amount_paid": float(request.amount_paid),
            "currency": request.currency,
            "paid_at": request.paid_at or self._utc_now_iso(),
            "payment_method": request.payment_method,
            "payment_reference": request.payment_reference,
            "created_at": self._utc_now_iso(),
        }
        return self._repo.create_receipt(customer_id, receipt)

    def get_receipt(self, customer_id: str, receipt_id: str) -> dict:
        receipt = self._repo.get_receipt(customer_id, receipt_id)
        if not receipt:
            raise ReceiptNotFoundError(f"Receipt '{receipt_id}' does not exist for customer '{customer_id}'")
        return receipt

    def list_receipts(self, customer_id: str) -> list[dict]:
        return self._repo.list_receipts(customer_id)


class OpenSearchBillingRepository:
    def __init__(self, client: OpenSearchClient):
        self._client = client
        self._invoices_index = "customer-invoices"
        self._receipts_index = "customer-receipts"

    def create_invoice(self, customer_id: str, invoice: dict) -> dict:
        self._ensure_index_exists(self._invoices_index)
        self._client._http_json("PUT", f"/{self._invoices_index}/_doc/{parse.quote(invoice['invoice_id'])}", invoice)
        return invoice

    @staticmethod
    def _is_missing_index(exc: OpenSearchApiError) -> bool:
        return exc.status_code == 404 and "no such index" in str(exc).lower()

    @staticmethod
    def _is_resource_already_exists(exc: OpenSearchApiError) -> bool:
        body = (exc.body or "").lower()
        message = str(exc).lower()
        return exc.status_code == 400 and (
            "resource_already_exists_exception" in body
            or "resource_already_exists_exception" in message
        )

    def _ensure_index_exists(self, index_name: str) -> None:
        try:
            self._client._http_json("PUT", f"/{index_name}")
        except OpenSearchApiError as exc:
            if self._is_resource_already_exists(exc):
                return
            raise

    def list_invoices(self, customer_id: str) -> list[dict]:
        try:
            payload = self._client._http_json(
                "GET",
                f"/{self._invoices_index}/_search",
                {
                    "query": {"term": {"customer_id": customer_id}},
                    "sort": [{"created_at": "desc"}],
                    "size": 500,
                },
            )
        except OpenSearchApiError as exc:
            if self._is_missing_index(exc):
                return []
            raise
        return [hit.get("_source", {}) for hit in payload.get("hits", {}).get("hits", [])]

    def get_invoice(self, customer_id: str, invoice_id: str) -> dict | None:
        try:
            payload = self._client._http_json(
                "GET",
                f"/{self._invoices_index}/_search",
                {
                    "query": {
                        "bool": {
                            "filter": [
                                {"term": {"customer_id": customer_id}},
                                {"term": {"invoice_id": invoice_id}},
                            ]
                        }
                    },
                    "size": 1,
                },
            )
        except OpenSearchApiError as exc:
            if self._is_missing_index(exc):
                return None
            raise
        hits = payload.get("hits", {}).get("hits", [])
        return hits[0].get("_source") if hits else None

    def save_invoice(self, customer_id: str, invoice: dict) -> dict:
        self._ensure_index_exists(self._invoices_index)
        self._client._http_json("PUT", f"/{self._invoices_index}/_doc/{parse.quote(invoice['invoice_id'])}", invoice)
        return invoice

    def delete_invoice(self, customer_id: str, invoice_id: str) -> bool:
        invoice = self.get_invoice(customer_id, invoice_id)
        if not invoice:
            return False
        self._client._http_json("DELETE", f"/{self._invoices_index}/_doc/{parse.quote(invoice_id)}")
        return True

    def create_receipt(self, customer_id: str, receipt: dict) -> dict:
        self._ensure_index_exists(self._receipts_index)
        self._client._http_json("PUT", f"/{self._receipts_index}/_doc/{parse.quote(receipt['receipt_id'])}", receipt)
        return receipt

    def get_receipt(self, customer_id: str, receipt_id: str) -> dict | None:
        try:
            payload = self._client._http_json(
                "GET",
                f"/{self._receipts_index}/_search",
                {
                    "query": {
                        "bool": {
                            "filter": [
                                {"term": {"customer_id": customer_id}},
                                {"term": {"receipt_id": receipt_id}},
                            ]
                        }
                    },
                    "size": 1,
                },
            )
        except OpenSearchApiError as exc:
            if self._is_missing_index(exc):
                return None
            raise
        hits = payload.get("hits", {}).get("hits", [])
        return hits[0].get("_source") if hits else None

    def list_receipts(self, customer_id: str) -> list[dict]:
        try:
            payload = self._client._http_json(
                "GET",
                f"/{self._receipts_index}/_search",
                {
                    "query": {"term": {"customer_id": customer_id}},
                    "sort": [{"created_at": "desc"}],
                    "size": 500,
                },
            )
        except OpenSearchApiError as exc:
            if self._is_missing_index(exc):
                return []
            raise
        return [hit.get("_source", {}) for hit in payload.get("hits", {}).get("hits", [])]
