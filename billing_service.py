import datetime as dt
import threading
import uuid
from dataclasses import dataclass
from typing import Protocol

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


class BillingRepository(Protocol):
    def create_invoice(self, project_id: str, invoice: dict) -> dict: ...
    def list_invoices(self, project_id: str) -> list[dict]: ...
    def get_invoice(self, project_id: str, invoice_id: str) -> dict | None: ...
    def save_invoice(self, project_id: str, invoice: dict) -> dict: ...
    def create_receipt(self, project_id: str, receipt: dict) -> dict: ...
    def get_receipt(self, project_id: str, receipt_id: str) -> dict | None: ...
    def list_receipts(self, project_id: str) -> list[dict]: ...


class InMemoryBillingRepository:
    """Simple storage adapter to keep billing flows isolated from HTTP handlers."""

    def __init__(self):
        self._lock = threading.Lock()
        self._invoices: dict[str, dict[str, dict]] = {}
        self._receipts: dict[str, dict[str, dict]] = {}

    def create_invoice(self, project_id: str, invoice: dict) -> dict:
        with self._lock:
            self._invoices.setdefault(project_id, {})[invoice["invoice_id"]] = invoice
        return invoice

    def list_invoices(self, project_id: str) -> list[dict]:
        with self._lock:
            return list(self._invoices.get(project_id, {}).values())

    def get_invoice(self, project_id: str, invoice_id: str) -> dict | None:
        with self._lock:
            return self._invoices.get(project_id, {}).get(invoice_id)

    def save_invoice(self, project_id: str, invoice: dict) -> dict:
        with self._lock:
            self._invoices.setdefault(project_id, {})[invoice["invoice_id"]] = invoice
        return invoice

    def create_receipt(self, project_id: str, receipt: dict) -> dict:
        with self._lock:
            self._receipts.setdefault(project_id, {})[receipt["receipt_id"]] = receipt
        return receipt

    def get_receipt(self, project_id: str, receipt_id: str) -> dict | None:
        with self._lock:
            return self._receipts.get(project_id, {}).get(receipt_id)

    def list_receipts(self, project_id: str) -> list[dict]:
        with self._lock:
            return list(self._receipts.get(project_id, {}).values())


class OpenSearchBillingRepository:
    def __init__(self, debug: bool = False):
        self._client = OpenSearchClient(debug=debug)
        self._client.create_billing_indexes()

    @staticmethod
    def _extract_source(hit: dict) -> dict:
        return hit.get("_source", {})

    def create_invoice(self, project_id: str, invoice: dict) -> dict:
        self._client.upsert_invoice(invoice["invoice_id"], invoice)
        return invoice

    def list_invoices(self, project_id: str) -> list[dict]:
        result = self._client.search_project_invoices(project_id)
        return [self._extract_source(hit) for hit in result.get("hits", {}).get("hits", [])]

    def get_invoice(self, project_id: str, invoice_id: str) -> dict | None:
        try:
            invoice = self._extract_source(self._client.get_invoice(invoice_id))
        except OpenSearchApiError as exc:
            if exc.status_code == 404:
                return None
            raise
        if invoice.get("project_id") != project_id:
            return None
        return invoice

    def save_invoice(self, project_id: str, invoice: dict) -> dict:
        self._client.upsert_invoice(invoice["invoice_id"], invoice)
        return invoice

    def create_receipt(self, project_id: str, receipt: dict) -> dict:
        self._client.upsert_receipt(receipt["receipt_id"], receipt)
        return receipt

    def get_receipt(self, project_id: str, receipt_id: str) -> dict | None:
        try:
            receipt = self._extract_source(self._client.get_receipt(receipt_id))
        except OpenSearchApiError as exc:
            if exc.status_code == 404:
                return None
            raise
        if receipt.get("project_id") != project_id:
            return None
        return receipt

    def list_receipts(self, project_id: str) -> list[dict]:
        result = self._client.search_project_receipts(project_id)
        return [self._extract_source(hit) for hit in result.get("hits", {}).get("hits", [])]


class BillingService:
    def __init__(self, repository: BillingRepository):
        self._repo = repository

    @staticmethod
    def _utc_now_iso() -> str:
        return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()

    def create_invoice(self, project_id: str, request: InvoiceCreateRequest) -> dict:
        created_at = self._utc_now_iso()
        invoice = {
            "invoice_id": f"inv_{uuid.uuid4().hex[:16]}",
            "project_id": project_id,
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
        return self._repo.create_invoice(project_id, invoice)

    def list_invoices(self, project_id: str) -> list[dict]:
        return self._repo.list_invoices(project_id)

    def get_invoice(self, project_id: str, invoice_id: str) -> dict:
        invoice = self._repo.get_invoice(project_id, invoice_id)
        if not invoice:
            raise InvoiceNotFoundError(f"Invoice '{invoice_id}' does not exist for project '{project_id}'")
        return invoice

    def create_receipt(self, project_id: str, request: ReceiptCreateRequest) -> dict:
        invoice = self.get_invoice(project_id, request.invoice_id)
        invoice["amount_paid"] = float(invoice.get("amount_paid", 0.0)) + float(request.amount_paid)
        if invoice["amount_paid"] >= float(invoice["amount_due"]):
            invoice["status"] = "paid"
        else:
            invoice["status"] = "partially_paid"
        invoice["updated_at"] = self._utc_now_iso()
        self._repo.save_invoice(project_id, invoice)

        receipt = {
            "receipt_id": f"rcpt_{uuid.uuid4().hex[:16]}",
            "project_id": project_id,
            "invoice_id": request.invoice_id,
            "amount_paid": float(request.amount_paid),
            "currency": request.currency,
            "paid_at": request.paid_at or self._utc_now_iso(),
            "payment_method": request.payment_method,
            "payment_reference": request.payment_reference,
            "created_at": self._utc_now_iso(),
        }
        return self._repo.create_receipt(project_id, receipt)

    def get_receipt(self, project_id: str, receipt_id: str) -> dict:
        receipt = self._repo.get_receipt(project_id, receipt_id)
        if not receipt:
            raise ReceiptNotFoundError(f"Receipt '{receipt_id}' does not exist for project '{project_id}'")
        return receipt

    def list_receipts(self, project_id: str) -> list[dict]:
        return self._repo.list_receipts(project_id)
