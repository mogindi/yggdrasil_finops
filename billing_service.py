import datetime as dt
import threading
import uuid
from dataclasses import dataclass


class BillingError(Exception):
    pass


class InvoiceNotFoundError(BillingError):
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

    def list_receipts(self, project_id: str) -> list[dict]:
        with self._lock:
            return list(self._receipts.get(project_id, {}).values())


class BillingService:
    def __init__(self, repository: InMemoryBillingRepository):
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

    def list_receipts(self, project_id: str) -> list[dict]:
        return self._repo.list_receipts(project_id)
