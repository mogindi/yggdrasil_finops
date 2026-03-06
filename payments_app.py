#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import logging
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from opensearch_client import OpenSearchApiError, OpenSearchClient, OpenSearchError
from cloudkitty_client import CloudKittyClient, CloudKittyError, OpenStackAuthError, ProjectNotFoundError
from currency import get_default_currency
from startup_validation import describe_env, env_flag_enabled, print_env_resolution, validate_http_endpoint


DEBUG_MODE = False
LOGGER = logging.getLogger("payments_app")


def _payments_partition(project_id: str) -> str:
    return f"project:{project_id}"


def _parse_as_of_date(raw: str | None) -> dt.datetime | None:
    if not raw:
        return None
    parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _end_of_last_month(now: dt.datetime) -> dt.datetime:
    month_start = now.astimezone(dt.timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return month_start - dt.timedelta(seconds=1)


def _compute_project_balance(project_id: str, as_of: dt.datetime) -> dict:
    costs_client = CloudKittyClient(debug=DEBUG_MODE)
    payments_client = OpenSearchClient(debug=DEBUG_MODE)

    costs_client.ensure_project_exists(project_id)
    project_created_at = costs_client.get_project_created_at(project_id)
    start = (project_created_at or as_of).astimezone(dt.timezone.utc)
    costs_total = costs_client.get_project_aggregate_for_range(project_id, start, as_of)

    payments_totals = payments_client.get_total_paid(project_id, paid_before=as_of.isoformat())
    payments_total = float(payments_totals.get("aggregations", {}).get("total_paid", {}).get("value", 0.0) or 0.0)

    balance = float(costs_total) - payments_total
    return {
        "_index": "project-balances",
        "_id": project_id,
        "found": True,
        "_source": {
            "project_id": project_id,
            "currency": get_default_currency(),
            "costs_total": float(costs_total),
            "payments_total": payments_total,
            "balance": balance,
            "paid_total": payments_total,
            "refunded_total": float(costs_total),
            "net_paid": -balance,
            "updated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
            "as_of_date": as_of.isoformat(),
        },
    }


class PaymentsHandler(BaseHTTPRequestHandler):
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
            return self._json({"status": "ok", "service": "payments"})
        parts = parsed.path.split("/")
        if len(parts) >= 5 and parts[1] == "api" and parts[2] == "projects" and parts[4] == "payments":
            return self._project_payments_get(parts[3], parts, parse_qs(parsed.query))
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self):
        self._log_api_request()
        parts = urlparse(self.path).path.split("/")
        if len(parts) >= 5 and parts[1] == "api" and parts[2] == "projects" and parts[4] == "payments":
            return self._project_payments_post(parts[3], parts)
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_PUT(self):
        self._log_api_request()
        parts = urlparse(self.path).path.split("/")
        if len(parts) >= 5 and parts[1] == "api" and parts[2] == "projects" and parts[4] == "payments":
            return self._project_payments_put(parts[3], parts)
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length).decode("utf-8")) if length > 0 else {}

    def _project_payments_get(self, project_id: str, parts: list[str], query: dict[str, list[str]]):
        client = OpenSearchClient(debug=DEBUG_MODE)
        partition = _payments_partition(project_id)
        try:
            if len(parts) == 5:
                return self._json(client.search_project_payments(project_id, size=int(query.get("size", ["25"])[0])))
            if len(parts) == 7 and parts[5] == "events":
                return self._json(client.get_payment_event(partition, parts[6]))
            if len(parts) == 7 and parts[5] == "invoices":
                return self._json(client.search_project_invoice_payments(project_id, parts[6]))
            if len(parts) == 6 and parts[5] == "total-paid":
                return self._json(client.get_total_paid(project_id))
            if len(parts) == 6 and parts[5] == "balance":
                raw_as_of = query.get("as_of_date", [None])[0]
                try:
                    as_of = _parse_as_of_date(raw_as_of)
                except ValueError:
                    return self._json({"error": "as_of_date must be an ISO8601 datetime"}, status=400)
                effective_as_of = as_of or _end_of_last_month(dt.datetime.now(dt.timezone.utc))
                try:
                    return self._json(_compute_project_balance(project_id, effective_as_of))
                except ProjectNotFoundError as exc:
                    return self._json({"error": str(exc)}, status=404)
                except (OpenStackAuthError, CloudKittyError) as exc:
                    return self._json({"error": str(exc)}, status=502)
            if len(parts) == 6 and parts[5] == "mapping":
                return self._json(client.get_index_mapping(partition))
            if len(parts) == 6 and parts[5] == "settings":
                return self._json(client.get_index_settings(partition))
        except (OpenSearchApiError, OpenSearchError) as exc:
            return self._json({"error": str(exc), "opensearch_url": client.endpoint}, status=502)
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def _project_payments_post(self, project_id: str, parts: list[str]):
        client = OpenSearchClient(debug=DEBUG_MODE)
        partition = _payments_partition(project_id)
        try:
            if len(parts) == 6 and parts[5] == "setup":
                return self._json({"template": client.create_payments_template(), "payments_index": client.create_payments_index(partition), "balances_index": client.create_balances_index()}, status=201)
            if len(parts) == 7 and parts[5] == "events" and parts[6] == "bulk":
                body = self._read_json_body()
                events = body.get("events", [])
                for event in events:
                    event["project_id"] = project_id
                return self._json(client.bulk_payment_events(events, partition), status=201)
            if len(parts) == 6 and parts[5] == "refresh":
                return self._json(client.refresh_index(partition))
        except (OpenSearchApiError, OpenSearchError) as exc:
            return self._json({"error": str(exc), "opensearch_url": client.endpoint}, status=502)
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def _project_payments_put(self, project_id: str, parts: list[str]):
        client = OpenSearchClient(debug=DEBUG_MODE)
        partition = _payments_partition(project_id)
        try:
            if len(parts) == 7 and parts[5] == "events":
                body = self._read_json_body()
                body["project_id"] = project_id
                body.setdefault("ingested_at", dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat())
                return self._json(client.upsert_payment_event(partition, parts[6], body), status=201)
            if len(parts) == 6 and parts[5] == "balance":
                body = self._read_json_body()
                costs_total = float(body.get("costs_total", body.get("due_total", 0)))
                payments_total = float(body.get("payments_total", body.get("paid_total", 0)))
                return self._json(
                    client.upsert_balance(
                        project_id,
                        body.get("currency", get_default_currency()),
                        costs_total,
                        payments_total,
                    ),
                    status=201,
                )
        except (OpenSearchApiError, OpenSearchError) as exc:
            return self._json({"error": str(exc), "opensearch_url": client.endpoint}, status=502)
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def _json(self, payload: dict, status: int = 200):
        body = json.dumps(payload).encode("utf-8")
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


def run() -> None:
    parser = argparse.ArgumentParser(description="Payments service")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()
    global DEBUG_MODE
    DEBUG_MODE = args.debug or env_flag_enabled("DEBUG", default=False)
    if DEBUG_MODE:
        logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    opensearch_url, using_default = describe_env("OPENSEARCH_URL")
    print_env_resolution("OPENSEARCH_URL", opensearch_url, using_default)
    validate_http_endpoint("OPENSEARCH_URL", opensearch_url, health_path="/")

    os_verify, os_verify_defaulted = describe_env("OS_VERIFY")
    print_env_resolution("OS_VERIFY", os_verify, os_verify_defaulted)

    ThreadingHTTPServer(("0.0.0.0", args.port), PaymentsHandler).serve_forever()


if __name__ == "__main__":
    run()
