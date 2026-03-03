#!/usr/bin/env python3
import datetime as dt
import html
import json
import os
import argparse
import logging
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from billing_service import BillingError, BillingService, InMemoryBillingRepository, InvoiceCreateRequest, InvoiceNotFoundError, ReceiptCreateRequest, ReceiptNotFoundError
from brevo_client import BrevoClient, BrevoError
from cloudkitty_client import CloudKittyClient, CloudKittyError, OpenStackAuthError, ProjectNotFoundError
from currency import get_default_currency
from startup_validation import describe_env, print_env_resolution
from document_service import DocumentError, DocumentService
from opensearch_client import OpenSearchApiError, OpenSearchClient, OpenSearchError
from revolut_client import RevolutApiError, RevolutBusinessClient, RevolutError


ROOT = Path(__file__).resolve().parent
DEBUG_MODE = False
BILLING_SERVICE = BillingService(InMemoryBillingRepository())
DOCUMENT_SERVICE = DocumentService()
ENABLED_DOMAINS = {
    item.strip().lower()
    for item in os.environ.get("ENABLED_DOMAINS", "costs,document_generator,checkout,payments,ui").split(",")
    if item.strip()
}


def _domain_enabled(name: str) -> bool:
    return name.lower() in ENABLED_DOMAINS


def _parse_date(raw: str | None, default: dt.datetime) -> dt.datetime:
    if not raw:
        return default
    parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _last_month_bounds(now: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    month_start = now.astimezone(dt.timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_month_end = month_start - dt.timedelta(seconds=1)
    last_month_start = last_month_end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return last_month_start, last_month_end


def _month_bounds_utc(year: int, month: int) -> tuple[dt.datetime, dt.datetime]:
    start = dt.datetime(year, month, 1, 0, 0, 0, tzinfo=dt.timezone.utc)
    if month == 12:
        next_month_start = dt.datetime(year + 1, 1, 1, 0, 0, 0, tzinfo=dt.timezone.utc)
    else:
        next_month_start = dt.datetime(year, month + 1, 1, 0, 0, 0, tzinfo=dt.timezone.utc)
    end = next_month_start - dt.timedelta(seconds=1)
    return start, end


def _start_of_current_month_utc(now: dt.datetime) -> dt.datetime:
    return now.astimezone(dt.timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _payments_partition(project_id: str) -> str:
    return f"project:{project_id}"


def _start_of_month_utc(moment: dt.datetime) -> dt.datetime:
    return moment.astimezone(dt.timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


class CostHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/" and _domain_enabled("ui"):
            return self._serve_file(ROOT / "templates" / "index.html", "text/html")
        if parsed.path == "/healthz":
            return self._json({"status": "ok"})
        if parsed.path.startswith("/static/") and _domain_enabled("ui"):
            return self._serve_file(ROOT / parsed.path.lstrip("/"), self._content_type(parsed.path))
        if parsed.path.startswith("/api/projects/"):
            parts = parsed.path.split("/")
            if _domain_enabled("costs") and len(parts) == 5 and parts[1] == "api" and parts[2] == "projects" and parts[4] == "costs":
                project_id = parts[3]
                return self._project_costs(project_id, parse_qs(parsed.query))
            if _domain_enabled("costs") and len(parts) == 6 and parts[1] == "api" and parts[2] == "projects" and parts[4] == "costs":
                project_id = parts[3]
                if parts[5] == "last-month":
                    return self._project_costs_last_month(project_id, parse_qs(parsed.query))
                if parts[5] == "monthly":
                    return self._project_costs_monthly(project_id)
                return self._project_costs_for_month(project_id, parts[5], parse_qs(parsed.query))
            if _domain_enabled("costs") and len(parts) == 7 and parts[1] == "api" and parts[2] == "projects" and parts[4] == "costs":
                project_id = parts[3]
                if parts[5] == "monthly" and parts[6] == "graph":
                    return self._project_costs_monthly_graph(project_id)

            if _domain_enabled("document_generator") and len(parts) >= 5 and parts[1] == "api" and parts[2] == "projects" and parts[4] == "invoices":
                project_id = parts[3]
                return self._project_invoices_get(project_id, parts)
            if _domain_enabled("document_generator") and len(parts) >= 5 and parts[1] == "api" and parts[2] == "projects" and parts[4] == "receipts":
                project_id = parts[3]
                return self._project_receipts_get(project_id, parts)
            if _domain_enabled("payments") and len(parts) >= 5 and parts[1] == "api" and parts[2] == "projects" and parts[4] == "payments":
                project_id = parts[3]
                return self._project_payments_get(project_id, parts, parse_qs(parsed.query))

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self):
        parsed = urlparse(self.path)
        parts = parsed.path.split("/")
        if _domain_enabled("document_generator") and len(parts) >= 5 and parts[1] == "api" and parts[2] == "projects" and parts[4] == "invoices":
            project_id = parts[3]
            return self._project_invoices_post(project_id)
        if _domain_enabled("document_generator") and len(parts) >= 5 and parts[1] == "api" and parts[2] == "projects" and parts[4] == "receipts":
            project_id = parts[3]
            return self._project_receipts_post(project_id)
        if _domain_enabled("checkout") and len(parts) >= 5 and parts[1] == "api" and parts[2] == "projects" and parts[4] == "payments":
            project_id = parts[3]
            if len(parts) == 7 and parts[5] == "revolut" and parts[6] == "order":
                return self._project_payments_revolut_create(project_id)
        if _domain_enabled("payments") and len(parts) >= 5 and parts[1] == "api" and parts[2] == "projects" and parts[4] == "payments":
            project_id = parts[3]
            return self._project_payments_post(project_id, parts, parse_qs(parsed.query))
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_PUT(self):
        parsed = urlparse(self.path)
        parts = parsed.path.split("/")
        if _domain_enabled("payments") and len(parts) >= 5 and parts[1] == "api" and parts[2] == "projects" and parts[4] == "payments":
            project_id = parts[3]
            return self._project_payments_put(project_id, parts, parse_qs(parsed.query))
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _ensure_cloudkitty_project_exists(self, project_id: str):
        client = CloudKittyClient(debug=DEBUG_MODE)
        try:
            client.ensure_project_exists(project_id)
        except ProjectNotFoundError as exc:
            self._json({"error": str(exc)}, status=404)
            return False
        except (OpenStackAuthError, CloudKittyError) as exc:
            self._json({"error": str(exc)}, status=502)
            return False
        return True


    def _project_invoices_get(self, project_id: str, parts: list[str]):
        if not self._ensure_cloudkitty_project_exists(project_id):
            return
        try:
            if len(parts) == 5:
                return self._json({"invoices": BILLING_SERVICE.list_invoices(project_id)})
            if len(parts) == 6:
                return self._json(BILLING_SERVICE.get_invoice(project_id, parts[5]))
            if len(parts) == 7 and parts[6] == "file":
                return self._project_invoice_file_get(project_id, parts[5])
        except InvoiceNotFoundError as exc:
            return self._json({"error": str(exc)}, status=404)
        except BillingError as exc:
            return self._json({"error": str(exc)}, status=400)

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def _project_invoices_post(self, project_id: str):
        if not self._ensure_cloudkitty_project_exists(project_id):
            return
        body = self._read_json_body()
        try:
            request = InvoiceCreateRequest(
                amount_due=float(body.get("amount_due", 0)),
                currency=body.get("currency", get_default_currency()),
                customer_name=body.get("customer_name", ""),
                customer_email=body.get("customer_email", ""),
                due_at=body.get("due_at"),
                description=body.get("description", ""),
            )
            return self._json(BILLING_SERVICE.create_invoice(project_id, request), status=201)
        except (TypeError, ValueError) as exc:
            return self._json({"error": f"Invalid invoice payload: {exc}"}, status=400)

    def _project_receipts_get(self, project_id: str, parts: list[str]):
        if not self._ensure_cloudkitty_project_exists(project_id):
            return
        try:
            if len(parts) == 5:
                return self._json({"receipts": BILLING_SERVICE.list_receipts(project_id)})
            if len(parts) == 6:
                return self._json(BILLING_SERVICE.get_receipt(project_id, parts[5]))
            if len(parts) == 7 and parts[6] == "file":
                return self._project_receipt_file_get(project_id, parts[5])
        except ReceiptNotFoundError as exc:
            return self._json({"error": str(exc)}, status=404)
        except BillingError as exc:
            return self._json({"error": str(exc)}, status=400)

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def _project_receipts_post(self, project_id: str):
        if not self._ensure_cloudkitty_project_exists(project_id):
            return
        body = self._read_json_body()
        try:
            request = ReceiptCreateRequest(
                invoice_id=body.get("invoice_id", ""),
                amount_paid=float(body.get("amount_paid", 0)),
                currency=body.get("currency", get_default_currency()),
                paid_at=body.get("paid_at"),
                payment_method=body.get("payment_method", "unknown"),
                payment_reference=body.get("payment_reference", ""),
            )
            return self._json(BILLING_SERVICE.create_receipt(project_id, request), status=201)
        except InvoiceNotFoundError as exc:
            return self._json({"error": str(exc)}, status=404)
        except (TypeError, ValueError, BillingError) as exc:
            return self._json({"error": f"Invalid receipt payload: {exc}"}, status=400)


    def _project_payments_revolut_create(self, project_id: str):
        if not self._ensure_cloudkitty_project_exists(project_id):
            return
        body = self._read_json_body()
        invoice_id = body.get("invoice_id", "")
        if not invoice_id:
            return self._json({"error": "invoice_id is required"}, status=400)

        try:
            invoice = BILLING_SERVICE.get_invoice(project_id, invoice_id)
        except InvoiceNotFoundError as exc:
            return self._json({"error": str(exc)}, status=404)

        remaining_amount = float(invoice["amount_due"]) - float(invoice["amount_paid"])
        if remaining_amount <= 0:
            return self._json({"error": f"Invoice '{invoice_id}' is already fully paid"}, status=400)

        client = RevolutBusinessClient()
        try:
            response = client.create_order(
                order_id=invoice_id,
                amount=float(body.get("amount", remaining_amount)),
                currency=body.get("currency", invoice.get("currency", get_default_currency())),
                description=body.get("description", invoice.get("description", "Project invoice payment")),
                customer_email=invoice.get("customer_email", ""),
                success_url=body.get("success_url"),
                metadata={
                    "project_id": project_id,
                    "invoice_id": invoice_id,
                },
            )
            return self._json(response, status=201)
        except RevolutApiError as exc:
            return self._json({"error": str(exc), "details": exc.body}, status=502)
        except RevolutError as exc:
            return self._json({"error": str(exc)}, status=502)

    def _project_payments_get(self, project_id: str, parts: list[str], query: dict[str, list[str]]):
        if not self._ensure_cloudkitty_project_exists(project_id):
            return
        client = OpenSearchClient(debug=DEBUG_MODE)
        payments_partition = _payments_partition(project_id)
        try:
            if len(parts) == 5:
                size = int(query.get("size", ["25"])[0])
                return self._json(client.search_project_payments(project_id, size=size))
            if len(parts) == 7 and parts[5] == "events":
                return self._json(client.get_payment_event(payments_partition, parts[6]))
            if len(parts) == 7 and parts[5] == "invoices":
                return self._json(client.search_project_invoice_payments(project_id, parts[6]))
            if len(parts) == 6 and parts[5] == "total-paid":
                return self._json(client.get_total_paid(project_id))
            if len(parts) == 6 and parts[5] == "balance":
                return self._json(client.get_balance(project_id))
            if len(parts) == 6 and parts[5] == "mapping":
                return self._json(client.get_index_mapping(payments_partition))
            if len(parts) == 6 and parts[5] == "settings":
                return self._json(client.get_index_settings(payments_partition))
        except (OpenSearchApiError, OpenSearchError) as exc:
            return self._json({"error": str(exc), "opensearch_url": client.endpoint}, status=502)

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def _project_payments_post(self, project_id: str, parts: list[str], query: dict[str, list[str]]):
        if not self._ensure_cloudkitty_project_exists(project_id):
            return
        client = OpenSearchClient(debug=DEBUG_MODE)
        payments_partition = _payments_partition(project_id)
        try:
            if len(parts) == 6 and parts[5] == "setup":
                payload = {
                    "template": client.create_payments_template(),
                    "payments_index": client.create_payments_index(payments_partition),
                    "balances_index": client.create_balances_index(),
                }
                return self._json(payload, status=201)
            if len(parts) == 7 and parts[5] == "events" and parts[6] == "bulk":
                body = self._read_json_body()
                events = body.get("events", [])
                for event in events:
                    event["project_id"] = project_id
                return self._json(client.bulk_payment_events(events, payments_partition), status=201)
            if len(parts) == 6 and parts[5] == "refresh":
                return self._json(client.refresh_index(payments_partition))
        except (OpenSearchApiError, OpenSearchError) as exc:
            return self._json({"error": str(exc), "opensearch_url": client.endpoint}, status=502)

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def _project_payments_put(self, project_id: str, parts: list[str], query: dict[str, list[str]]):
        if not self._ensure_cloudkitty_project_exists(project_id):
            return
        client = OpenSearchClient(debug=DEBUG_MODE)
        payments_partition = _payments_partition(project_id)
        try:
            if len(parts) == 7 and parts[5] == "events":
                body = self._read_json_body()
                body["project_id"] = project_id
                body.setdefault("ingested_at", dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat())
                return self._json(client.upsert_payment_event(payments_partition, parts[6], body), status=201)
            if len(parts) == 6 and parts[5] == "balance":
                body = self._read_json_body()
                return self._json(
                    client.upsert_balance(
                        project_id,
                        body.get("currency", get_default_currency()),
                        float(body.get("paid_total", 0)),
                        float(body.get("refunded_total", 0)),
                        float(body.get("net_paid", 0)),
                    ),
                    status=201,
                )
        except (OpenSearchApiError, OpenSearchError) as exc:
            return self._json({"error": str(exc), "opensearch_url": client.endpoint}, status=502)

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def _project_costs(self, project_id: str, query: dict[str, list[str]]):
        now = dt.datetime.now(dt.timezone.utc)
        start = _parse_date(query.get("start", [None])[0], now - dt.timedelta(days=30))
        end = _parse_date(query.get("end", [None])[0], now)
        resolution = query.get("resolution", ["day"])[0]
        include_series = query.get("include_series", ["true"])[0].lower() != "false"

        client = CloudKittyClient(debug=DEBUG_MODE)
        try:
            client.ensure_project_exists(project_id)
            aggregate = client.get_project_aggregate_for_range(project_id, start, end)
            series = client.get_project_time_series(project_id, start, end, resolution) if include_series else []
        except ProjectNotFoundError as exc:
            return self._json({"error": str(exc)}, status=404)
        except (OpenStackAuthError, CloudKittyError) as exc:
            return self._json({"error": str(exc)}, status=502)

        return self._json(
            {
                "project_id": project_id,
                "aggregate_cost_now": aggregate,
                "currency": get_default_currency(),
                "time_series": series,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "resolution": resolution,
            }
        )

    def _project_costs_last_month(self, project_id: str, query: dict[str, list[str]]):
        now = dt.datetime.now(dt.timezone.utc)
        start, end = _last_month_bounds(now)
        query_with_range = dict(query)
        query_with_range["start"] = [start.isoformat()]
        query_with_range["end"] = [end.isoformat()]
        return self._project_costs(project_id, query_with_range)

    def _project_costs_for_month(self, project_id: str, year_month: str, query: dict[str, list[str]]):
        try:
            parsed = dt.datetime.strptime(year_month, "%Y-%m")
            start, end = _month_bounds_utc(parsed.year, parsed.month)
        except ValueError:
            return self._json({"error": "Month must be in YYYY-MM format"}, status=400)
        query_with_range = dict(query)
        query_with_range["start"] = [start.isoformat()]
        query_with_range["end"] = [end.isoformat()]
        return self._project_costs(project_id, query_with_range)

    def _project_costs_monthly(self, project_id: str):
        now = dt.datetime.now(dt.timezone.utc)

        client = CloudKittyClient(debug=DEBUG_MODE)
        try:
            client.ensure_project_exists(project_id)
            project_created_at = client.get_project_created_at(project_id)
            start = _start_of_month_utc(project_created_at or now)
            end = now
            series = client.get_project_time_series(project_id, start, end, "month")
        except ProjectNotFoundError as exc:
            return self._json({"error": str(exc)}, status=404)
        except (OpenStackAuthError, CloudKittyError) as exc:
            return self._json({"error": str(exc)}, status=502)

        monthly_series = [point for point in series if _parse_date(point["timestamp"], end) >= start]
        aggregate = sum(point["cost"] for point in monthly_series)

        return self._json(
            {
                "project_id": project_id,
                "aggregate_cost_now": aggregate,
                "currency": get_default_currency(),
                "time_series": monthly_series,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "resolution": "month",
            }
        )

    def _project_costs_monthly_graph(self, project_id: str):
        now = dt.datetime.now(dt.timezone.utc)

        client = CloudKittyClient(debug=DEBUG_MODE)
        try:
            client.ensure_project_exists(project_id)
            project_created_at = client.get_project_created_at(project_id)
            start = _start_of_month_utc(project_created_at or now)
            end = now
            series = client.get_project_time_series(project_id, start, end, "month")
        except ProjectNotFoundError as exc:
            return self._json({"error": str(exc)}, status=404)
        except (OpenStackAuthError, CloudKittyError) as exc:
            return self._json({"error": str(exc)}, status=502)

        monthly_series = [point for point in series if _parse_date(point["timestamp"], end) >= start]
        labels = [dt.datetime.fromisoformat(point["timestamp"].replace("Z", "+00:00")).strftime("%Y-%m") for point in monthly_series]
        costs = [float(point["cost"]) for point in monthly_series]
        max_cost = max(costs) if costs else 1.0

        chart_width = 820
        chart_height = 320
        left_pad = 55
        bottom_pad = 35
        plot_width = chart_width - left_pad - 20
        plot_height = chart_height - 20 - bottom_pad

        points = []
        if costs:
            for idx, cost in enumerate(costs):
                x = left_pad + (plot_width * idx / max(len(costs) - 1, 1))
                y = 20 + (plot_height * (1 - (cost / max_cost if max_cost > 0 else 0)))
                points.append((x, y, labels[idx], cost))

        polyline = " ".join(f"{x:.1f},{y:.1f}" for x, y, _, _ in points)
        dots = "".join(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="#2563eb"><title>{label}: {cost:.2f}</title></circle>'
            for x, y, label, cost in points
        )
        x_labels = "".join(
            f'<text x="{x:.1f}" y="{chart_height - 10}" text-anchor="middle" font-size="11" fill="#4b5563">{html.escape(label)}</text>'
            for x, _, label, _ in points
        )
        y_labels = "".join(
            f'<text x="6" y="{20 + (plot_height * (i / 4)) + 4:.1f}" font-size="11" fill="#4b5563">{max_cost * (1 - i / 4):.2f}</text>'
            for i in range(5)
        )

        page = f"""<!DOCTYPE html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>Monthly Cost Graph - {html.escape(project_id)}</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 24px; color: #111827; }}
    .card {{ border: 1px solid #e5e7eb; border-radius: 8px; padding: 16px; max-width: 880px; }}
    .subtitle {{ color: #4b5563; font-size: 0.95rem; margin-top: -8px; }}
  </style>
</head>
<body>
  <div class=\"card\">
    <h1>Monthly Cost History</h1>
    <p class=\"subtitle\">Project: <code>{html.escape(project_id)}</code> • Currency: {html.escape(get_default_currency())}</p>
    <svg width=\"{chart_width}\" height=\"{chart_height}\" role=\"img\" aria-label=\"Monthly cloud cost graph\">
      <rect x=\"0\" y=\"0\" width=\"{chart_width}\" height=\"{chart_height}\" fill=\"white\" />
      <line x1=\"{left_pad}\" y1=\"20\" x2=\"{left_pad}\" y2=\"{chart_height - bottom_pad}\" stroke=\"#9ca3af\" />
      <line x1=\"{left_pad}\" y1=\"{chart_height - bottom_pad}\" x2=\"{chart_width - 20}\" y2=\"{chart_height - bottom_pad}\" stroke=\"#9ca3af\" />
      {y_labels}
      <polyline points=\"{polyline}\" fill=\"none\" stroke=\"#2563eb\" stroke-width=\"2\" />
      {dots}
      {x_labels}
    </svg>
    <p>Total months: {len(monthly_series)}</p>
  </div>
</body>
</html>"""
        return self._html(page)


    def _project_invoice_file_get(self, project_id: str, invoice_id: str):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        logo_path = query.get("logo_path", [None])[0]
        view = query.get("view", ["pdf"])[0]
        download = query.get("download", ["false"])[0].lower() == "true"
        send_email = query.get("send_email", ["false"])[0].lower() == "true"
        email_to = query.get("email", [None])[0]

        invoice = BILLING_SERVICE.get_invoice(project_id, invoice_id)
        try:
            pdf_bytes = DOCUMENT_SERVICE.build_invoice_pdf(invoice, logo_path=logo_path)
        except DocumentError as exc:
            return self._json({"error": str(exc)}, status=400)

        filename = f"{invoice_id}.pdf"
        if send_email:
            recipient = email_to or invoice.get("customer", {}).get("email", "")
            try:
                BrevoClient().send_pdf(
                    to_email=recipient,
                    subject=f"Invoice {invoice_id}",
                    html_content=f"<p>Please find invoice <b>{html.escape(invoice_id)}</b> attached.</p>",
                    filename=filename,
                    content=pdf_bytes,
                )
            except BrevoError as exc:
                return self._json({"error": str(exc)}, status=502)

        if view == "html":
            return self._html(DOCUMENT_SERVICE.build_pdf_html_page(f"Invoice {invoice_id}", filename, pdf_bytes))
        return self._pdf(pdf_bytes, filename=filename, download=download)

    def _project_receipt_file_get(self, project_id: str, receipt_id: str):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        logo_path = query.get("logo_path", [None])[0]
        view = query.get("view", ["pdf"])[0]
        download = query.get("download", ["false"])[0].lower() == "true"
        send_email = query.get("send_email", ["false"])[0].lower() == "true"
        email_to = query.get("email", [None])[0]

        receipt = BILLING_SERVICE.get_receipt(project_id, receipt_id)
        invoice = BILLING_SERVICE.get_invoice(project_id, receipt["invoice_id"])
        try:
            pdf_bytes = DOCUMENT_SERVICE.build_receipt_pdf(receipt, invoice, logo_path=logo_path)
        except DocumentError as exc:
            return self._json({"error": str(exc)}, status=400)

        filename = f"{receipt_id}.pdf"
        if send_email:
            recipient = email_to or invoice.get("customer", {}).get("email", "")
            try:
                BrevoClient().send_pdf(
                    to_email=recipient,
                    subject=f"Receipt {receipt_id}",
                    html_content=f"<p>Please find receipt <b>{html.escape(receipt_id)}</b> attached.</p>",
                    filename=filename,
                    content=pdf_bytes,
                )
            except BrevoError as exc:
                return self._json({"error": str(exc)}, status=502)

        if view == "html":
            return self._html(DOCUMENT_SERVICE.build_pdf_html_page(f"Receipt {receipt_id}", filename, pdf_bytes))
        return self._pdf(pdf_bytes, filename=filename, download=download)

    def _serve_file(self, path: Path, content_type: str):
        if not path.exists() or not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, status: int = 200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, payload: str, status: int = 200):
        body = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _pdf(self, payload: bytes, filename: str, download: bool = False, status: int = 200):
        self.send_response(status)
        disposition = "attachment" if download else "inline"
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Disposition", f'{disposition}; filename="{filename}"')
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    @staticmethod
    def _content_type(path: str) -> str:
        if path.endswith(".js"):
            return "application/javascript"
        if path.endswith(".css"):
            return "text/css"
        return "text/plain"


def run() -> None:
    parser = argparse.ArgumentParser(description="CloudKitty project cost viewer")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8082")), help="Port to bind the HTTP server to")
    parser.add_argument("--debug", action="store_true", help="Enable very verbose logging, including CloudKitty/Keystone API calls")
    args = parser.parse_args()

    global DEBUG_MODE
    DEBUG_MODE = args.debug
    if DEBUG_MODE:
        logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    for var_name, default in [
        ("OS_AUTH_URL", None),
        ("OS_USERNAME", None),
        ("OS_PASSWORD", None),
        ("OS_USER_DOMAIN_NAME", "Default"),
        ("OS_PROJECT_DOMAIN_NAME", "Default"),
        ("OS_INTERFACE", "public"),
        ("CLOUDKITTY_CURRENCY", "DKK"),
    ]:
        value, using_default = describe_env(var_name, default)
        display = "***" if var_name == "OS_PASSWORD" else value
        print_env_resolution(var_name, display, using_default)

    project_id = os.environ.get("OS_PROJECT_ID", "").strip()
    project_name = os.environ.get("OS_PROJECT_NAME", "").strip()
    if not project_id and not project_name:
        raise RuntimeError("Set OS_PROJECT_ID or OS_PROJECT_NAME")
    if project_id:
        print("[startup] OS_PROJECT_ID is set (environment)")
    if project_name:
        print("[startup] OS_PROJECT_NAME is set (environment)")

    try:
        CloudKittyClient(debug=DEBUG_MODE).validate_currency(get_default_currency())
    except (OpenStackAuthError, CloudKittyError) as exc:
        raise RuntimeError(f"CloudKitty currency validation failed: {exc}") from exc

    port = args.port
    server = ThreadingHTTPServer(("0.0.0.0", port), CostHandler)
    mode = "debug" if DEBUG_MODE else "normal"
    print(f"Serving on http://0.0.0.0:{port} ({mode} mode)")
    server.serve_forever()


if __name__ == "__main__":
    run()
