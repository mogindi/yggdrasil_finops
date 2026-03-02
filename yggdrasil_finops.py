#!/usr/bin/env python3
import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Any
from urllib import error, parse, request


DEFAULT_BASE_URL = os.environ.get("YGGDRASIL_FINOPS_API_URL", "http://localhost:8082")


@dataclass
class ApiResponse:
    status: int
    body: Any


class ApiClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def request_raw(self, method: str, path: str) -> tuple[int, bytes, dict[str, str]]:
        req = request.Request(f"{self.base_url}{path}", headers={"Accept": "*/*"}, method=method)
        try:
            with request.urlopen(req) as resp:
                return resp.status, resp.read(), dict(resp.headers.items())
        except error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers.items())

    def request_json(self, method: str, path: str, payload: dict[str, Any] | None = None) -> ApiResponse:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = request.Request(f"{self.base_url}{path}", data=data, headers=headers, method=method)
        try:
            with request.urlopen(req) as resp:
                text = resp.read().decode("utf-8")
                return ApiResponse(resp.status, json.loads(text) if text else {})
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8")
            parsed = {"error": body}
            if body:
                try:
                    parsed = json.loads(body)
                except json.JSONDecodeError:
                    parsed = {"error": body}
            return ApiResponse(exc.code, parsed)


def _print_response(resp: ApiResponse) -> int:
    print(json.dumps(resp.body, indent=2, sort_keys=True))
    return 0 if 200 <= resp.status < 300 else 1


def _add_base_and_project_args(parser: argparse.ArgumentParser, *, require_project: bool = True) -> None:
    parser.add_argument("--api-url", default=DEFAULT_BASE_URL, help="Base API URL (default: %(default)s)")
    if require_project:
        parser.add_argument("--project-id", required=True, help="Project identifier")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="yggdrasil_finops",
        description="CLI wrapper for yggdrasil_finops API",
    )
    subparsers = parser.add_subparsers(dest="resource", required=True)

    project_parser = subparsers.add_parser("project", help="Project level actions")
    project_sub = project_parser.add_subparsers(dest="action", required=True)
    project_setup = project_sub.add_parser("setup", help="Setup payment indices for a project")
    _add_base_and_project_args(project_setup)

    cost_parser = subparsers.add_parser("cost", help="Cost and usage actions")
    cost_sub = cost_parser.add_subparsers(dest="action", required=True)

    cost_aggregate = cost_sub.add_parser("aggregate", help="Fetch aggregate project costs")
    _add_base_and_project_args(cost_aggregate)
    cost_aggregate.add_argument("--start", help="ISO8601 start datetime")
    cost_aggregate.add_argument("--end", help="ISO8601 end datetime")
    cost_aggregate.add_argument("--resolution", default="month", help="Grouping resolution: hour/day/week/month")
    cost_aggregate.add_argument("--include-series", action="store_true", help="Include time series points")

    cost_last_month = cost_sub.add_parser("last-month", help="Fetch previous calendar month costs")
    _add_base_and_project_args(cost_last_month)
    cost_last_month.add_argument("--resolution", default="day", help="Grouping resolution: hour/day/week/month")
    cost_last_month.add_argument("--include-series", action="store_true", help="Include time series points")

    cost_month = cost_sub.add_parser("month", help="Fetch one specific calendar month costs")
    _add_base_and_project_args(cost_month)
    cost_month.add_argument("--month", required=True, help="Month in YYYY-MM format")
    cost_month.add_argument("--resolution", default="day", help="Grouping resolution: hour/day/week/month")
    cost_month.add_argument("--include-series", action="store_true", help="Include time series points")

    cost_monthly = cost_sub.add_parser("monthly", help="Fetch monthly history (excluding current month)")
    _add_base_and_project_args(cost_monthly)

    cost_monthly_graph = cost_sub.add_parser("monthly-graph", help="Render monthly usage graph HTML")
    _add_base_and_project_args(cost_monthly_graph)

    payment_parser = subparsers.add_parser("payment", help="Payment actions")
    payment_sub = payment_parser.add_subparsers(dest="action", required=True)

    payment_create = payment_sub.add_parser("create", help="Create or upsert a payment event")
    _add_base_and_project_args(payment_create)
    payment_create.add_argument("--event-id", required=True)
    payment_create.add_argument("--invoice-id", required=True)
    payment_create.add_argument("--amount", required=True, type=float)
    payment_create.add_argument("--currency", default="USD")
    payment_create.add_argument("--status", default="captured")
    payment_create.add_argument("--payment-direction", default="inbound")
    payment_create.add_argument("--paid-at", required=True, help="ISO8601 datetime")
    payment_create.add_argument("--provider", default="manual")
    payment_create.add_argument("--method", default="bank_transfer")
    payment_create.add_argument("--reference", default="")

    payment_list = payment_sub.add_parser("list", help="List payment events")
    _add_base_and_project_args(payment_list)
    payment_list.add_argument("--size", type=int, default=25)

    payment_show = payment_sub.add_parser("show", help="Show one payment event")
    _add_base_and_project_args(payment_show)
    payment_show.add_argument("--event-id", required=True)

    invoice_parser = subparsers.add_parser("invoice", help="Invoice actions")
    invoice_sub = invoice_parser.add_subparsers(dest="action", required=True)

    invoice_create = invoice_sub.add_parser("create", help="Create an invoice")
    _add_base_and_project_args(invoice_create)
    invoice_create.add_argument("--amount-due", required=True, type=float)
    invoice_create.add_argument("--currency", default="USD")
    invoice_create.add_argument("--customer-name", required=True)
    invoice_create.add_argument("--customer-email", required=True)
    invoice_create.add_argument("--due-at", required=True)
    invoice_create.add_argument("--description", default="")

    invoice_list = invoice_sub.add_parser("list", help="List invoices")
    _add_base_and_project_args(invoice_list)

    invoice_show = invoice_sub.add_parser("show", help="Show one invoice")
    _add_base_and_project_args(invoice_show)
    invoice_show.add_argument("--invoice-id", required=True)

    invoice_file = invoice_sub.add_parser("file", help="Generate/view/send invoice PDF")
    _add_base_and_project_args(invoice_file)
    invoice_file.add_argument("--invoice-id", required=True)
    invoice_file.add_argument("--logo-path", required=True)
    invoice_file.add_argument("--download-path", help="Save PDF to local path")
    invoice_file.add_argument("--html", action="store_true", help="Return HTML page with embedded PDF")
    invoice_file.add_argument("--send-email", action="store_true", help="Send PDF using Brevo API")
    invoice_file.add_argument("--email", help="Override recipient email")

    receipt_parser = subparsers.add_parser("receipt", help="Receipt actions")
    receipt_sub = receipt_parser.add_subparsers(dest="action", required=True)

    receipt_create = receipt_sub.add_parser("create", help="Create a receipt")
    _add_base_and_project_args(receipt_create)
    receipt_create.add_argument("--invoice-id", required=True)
    receipt_create.add_argument("--amount-paid", required=True, type=float)
    receipt_create.add_argument("--currency", default="USD")
    receipt_create.add_argument("--paid-at", required=True)
    receipt_create.add_argument("--payment-method", default="unknown")
    receipt_create.add_argument("--payment-reference", default="")

    receipt_list = receipt_sub.add_parser("list", help="List receipts")
    _add_base_and_project_args(receipt_list)

    receipt_file = receipt_sub.add_parser("file", help="Generate/view/send receipt PDF")
    _add_base_and_project_args(receipt_file)
    receipt_file.add_argument("--receipt-id", required=True)
    receipt_file.add_argument("--logo-path", required=True)
    receipt_file.add_argument("--download-path", help="Save PDF to local path")
    receipt_file.add_argument("--html", action="store_true", help="Return HTML page with embedded PDF")
    receipt_file.add_argument("--send-email", action="store_true", help="Send PDF using Brevo API")
    receipt_file.add_argument("--email", help="Override recipient email")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    client = ApiClient(args.api_url)

    if args.resource == "cost":
        if args.action == "aggregate":
            params = {
                "resolution": args.resolution,
                "include_series": "true" if args.include_series else "false",
            }
            if args.start:
                params["start"] = args.start
            if args.end:
                params["end"] = args.end
            query = parse.urlencode(params)
            return _print_response(client.request_json("GET", f"/api/projects/{args.project_id}/costs?{query}"))
        if args.action == "last-month":
            params = {
                "resolution": args.resolution,
                "include_series": "true" if args.include_series else "false",
            }
            query = parse.urlencode(params)
            return _print_response(client.request_json("GET", f"/api/projects/{args.project_id}/costs/last-month?{query}"))
        if args.action == "month":
            params = {
                "resolution": args.resolution,
                "include_series": "true" if args.include_series else "false",
            }
            query = parse.urlencode(params)
            return _print_response(client.request_json("GET", f"/api/projects/{args.project_id}/costs/{args.month}?{query}"))
        if args.action == "monthly":
            return _print_response(client.request_json("GET", f"/api/projects/{args.project_id}/costs/monthly"))
        if args.action == "monthly-graph":
            status, body, _headers = client.request_raw("GET", f"/api/projects/{args.project_id}/costs/monthly/graph")
            print(body.decode("utf-8", errors="ignore"))
            return 0 if 200 <= status < 300 else 1

    if args.resource == "project" and args.action == "setup":
        return _print_response(client.request_json("POST", f"/api/projects/{args.project_id}/payments/setup"))

    if args.resource == "payment" and args.action == "create":
        payload = {
            "invoice_id": args.invoice_id,
            "amount": args.amount,
            "currency": args.currency,
            "status": args.status,
            "payment_direction": args.payment_direction,
            "paid_at": args.paid_at,
            "provider": args.provider,
            "method": args.method,
            "reference": args.reference,
        }
        return _print_response(client.request_json("PUT", f"/api/projects/{args.project_id}/payments/events/{args.event_id}", payload))
    if args.resource == "payment" and args.action == "list":
        return _print_response(client.request_json("GET", f"/api/projects/{args.project_id}/payments?size={args.size}"))
    if args.resource == "payment" and args.action == "show":
        return _print_response(client.request_json("GET", f"/api/projects/{args.project_id}/payments/events/{args.event_id}"))

    if args.resource == "invoice" and args.action == "create":
        payload = {
            "amount_due": args.amount_due,
            "currency": args.currency,
            "customer_name": args.customer_name,
            "customer_email": args.customer_email,
            "due_at": args.due_at,
            "description": args.description,
        }
        return _print_response(client.request_json("POST", f"/api/projects/{args.project_id}/invoices", payload))
    if args.resource == "invoice" and args.action == "list":
        return _print_response(client.request_json("GET", f"/api/projects/{args.project_id}/invoices"))
    if args.resource == "invoice" and args.action == "show":
        return _print_response(client.request_json("GET", f"/api/projects/{args.project_id}/invoices/{args.invoice_id}"))
    if args.resource == "invoice" and args.action == "file":
        params = {
            "view": "html" if args.html else "pdf",
            "download": "true" if args.download_path else "false",
            "send_email": "true" if args.send_email else "false",
        }
        params["logo_path"] = args.logo_path
        if args.email:
            params["email"] = args.email
        query = parse.urlencode(params)
        status, body, headers = client.request_raw("GET", f"/api/projects/{args.project_id}/invoices/{args.invoice_id}/file?{query}")
        if args.download_path and 200 <= status < 300:
            with open(args.download_path, "wb") as fp:
                fp.write(body)
            print(json.dumps({"saved_to": args.download_path, "status": status}, indent=2, sort_keys=True))
            return 0
        content_type = headers.get("Content-Type", "")
        if content_type.startswith("text/html"):
            print(body.decode("utf-8"))
            return 0 if 200 <= status < 300 else 1
        if content_type.startswith("application/pdf"):
            encoded = body.hex()
            print(json.dumps({"status": status, "pdf_hex": encoded[:200]}, indent=2, sort_keys=True))
            return 0 if 200 <= status < 300 else 1
        print(body.decode("utf-8", errors="ignore"))
        return 0 if 200 <= status < 300 else 1

    if args.resource == "receipt" and args.action == "create":
        payload = {
            "invoice_id": args.invoice_id,
            "amount_paid": args.amount_paid,
            "currency": args.currency,
            "paid_at": args.paid_at,
            "payment_method": args.payment_method,
            "payment_reference": args.payment_reference,
        }
        return _print_response(client.request_json("POST", f"/api/projects/{args.project_id}/receipts", payload))
    if args.resource == "receipt" and args.action == "list":
        return _print_response(client.request_json("GET", f"/api/projects/{args.project_id}/receipts"))
    if args.resource == "receipt" and args.action == "file":
        params = {
            "view": "html" if args.html else "pdf",
            "download": "true" if args.download_path else "false",
            "send_email": "true" if args.send_email else "false",
        }
        params["logo_path"] = args.logo_path
        if args.email:
            params["email"] = args.email
        query = parse.urlencode(params)
        status, body, headers = client.request_raw("GET", f"/api/projects/{args.project_id}/receipts/{args.receipt_id}/file?{query}")
        if args.download_path and 200 <= status < 300:
            with open(args.download_path, "wb") as fp:
                fp.write(body)
            print(json.dumps({"saved_to": args.download_path, "status": status}, indent=2, sort_keys=True))
            return 0
        content_type = headers.get("Content-Type", "")
        if content_type.startswith("text/html"):
            print(body.decode("utf-8"))
            return 0 if 200 <= status < 300 else 1
        if content_type.startswith("application/pdf"):
            encoded = body.hex()
            print(json.dumps({"status": status, "pdf_hex": encoded[:200]}, indent=2, sort_keys=True))
            return 0 if 200 <= status < 300 else 1
        print(body.decode("utf-8", errors="ignore"))
        return 0 if 200 <= status < 300 else 1

    parser.error("Unsupported command")
    return 2


if __name__ == "__main__":
    sys.exit(main())
