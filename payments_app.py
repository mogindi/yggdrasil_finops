#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import logging
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import error, parse, request
from urllib.parse import parse_qs, urlparse

from opensearch_client import OpenSearchApiError, OpenSearchClient, OpenSearchError
from currency import get_default_currency
from startup_validation import describe_env, env_flag_enabled, print_env_resolution, validate_http_endpoint


DEBUG_MODE = False
LOGGER = logging.getLogger("payments_app")
COSTS_SERVICE_URL = os.environ.get("COSTS_SERVICE_URL", "http://localhost:8083").rstrip("/")
DOCUMENT_GENERATOR_SERVICE_URL = os.environ.get("DOCUMENT_GENERATOR_SERVICE_URL", "http://localhost:8084").rstrip("/")


class CostsServiceError(Exception):
    pass


class CostsServiceCustomerNotFoundError(CostsServiceError):
    pass


def _payments_partition(customer_id: str) -> str:
    return f"customer:{customer_id}"


def _parse_iso_date_or_datetime(raw: str | None, *, end_of_day_for_date_only: bool = False) -> dt.datetime | None:
    if not raw:
        return None
    normalized = raw.strip().replace("Z", "+00:00")
    if "T" not in normalized:
        parsed_date = dt.date.fromisoformat(normalized)
        if end_of_day_for_date_only:
            return dt.datetime(parsed_date.year, parsed_date.month, parsed_date.day, 23, 59, 59, tzinfo=dt.timezone.utc)
        return dt.datetime(parsed_date.year, parsed_date.month, parsed_date.day, 0, 0, 0, tzinfo=dt.timezone.utc)
    parsed = dt.datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _start_of_month(moment: dt.datetime) -> dt.datetime:
    return moment.astimezone(dt.timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _end_of_last_month(now: dt.datetime) -> dt.datetime:
    return _start_of_month(now) - dt.timedelta(seconds=1)


def _is_within_inclusive(value: dt.datetime, start: dt.datetime, end: dt.datetime) -> bool:
    return start <= value <= end


def _get_costs_total(customer_id: str, start: dt.datetime, end: dt.datetime) -> float:
    query = parse.urlencode({"start": start.isoformat(), "end": end.isoformat(), "include_series": "false"})
    url = f"{COSTS_SERVICE_URL}/api/customers/{parse.quote(customer_id)}/costs?{query}"
    req = request.Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with request.urlopen(req) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "{}")
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        if exc.code == 404:
            try:
                payload = json.loads(body) if body else {}
            except json.JSONDecodeError:
                payload = {}
            raise CostsServiceCustomerNotFoundError(payload.get("error") or f"Customer '{customer_id}' was not found") from exc
        raise CostsServiceError(f"costs service returned {exc.code}: {body or exc.reason}") from exc
    except error.URLError as exc:
        reason = getattr(exc, "reason", str(exc))
        raise CostsServiceError(f"costs service unavailable: {reason}") from exc

    return float(payload.get("aggregate_cost_now", 0.0) or 0.0)


def _get_customer_onboarding_start(customer_id: str, fallback_now: dt.datetime) -> dt.datetime:
    return _start_of_month(fallback_now)


def _fetch_invoices(customer_id: str) -> list[dict]:
    url = f"{DOCUMENT_GENERATOR_SERVICE_URL}/api/customers/{parse.quote(customer_id)}/invoices"
    req = request.Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with request.urlopen(req) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "{}")
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        raise CostsServiceError(f"document service returned {exc.code}: {body or exc.reason}") from exc
    except error.URLError as exc:
        reason = getattr(exc, "reason", str(exc))
        raise CostsServiceError(f"document service unavailable: {reason}") from exc

    invoices = payload.get("invoices", [])
    return invoices if isinstance(invoices, list) else []


def _compute_customer_balance(customer_id: str, costs_from: dt.datetime, costs_to: dt.datetime, payments_from: dt.datetime, payments_to: dt.datetime) -> dict:
    payments_client = OpenSearchClient(debug=DEBUG_MODE)
    costs_total = _get_costs_total(customer_id, costs_from, costs_to)

    payments_totals = payments_client.get_total_paid_by_customer(customer_id, created_from=payments_from.isoformat(), created_to=payments_to.isoformat())
    payments_total = float(payments_totals.get("aggregations", {}).get("total_paid", {}).get("value", 0.0) or 0.0)

    invoices_in_range: list[dict] = []
    for invoice in _fetch_invoices(customer_id):
        created_at = _parse_iso_date_or_datetime(invoice.get("created_at"))
        if created_at and _is_within_inclusive(created_at, costs_from, costs_to):
            invoices_in_range.append(invoice)
    invoices_total = sum(float(inv.get("amount_due", 0.0) or 0.0) for inv in invoices_in_range)

    payments_in_range = payments_client.list_payments_created_in_range_by_customer(customer_id, created_from=payments_from.isoformat(), created_to=payments_to.isoformat())
    payments_created_total = sum(float(item.get("amount", 0.0) or 0.0) for item in payments_in_range)

    balance = float(costs_total) - payments_total
    return {
        "_index": "customer-balances",
        "_id": customer_id,
        "found": True,
        "_source": {
            "customer_id": customer_id,
            "currency": get_default_currency(),
            "costs_total": float(costs_total),
            "payments_total": payments_total,
            "balance": balance,
            "paid_total": payments_total,
            "refunded_total": float(costs_total),
            "net_paid": -balance,
            "updated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
            "costs_from_date": costs_from.isoformat(),
            "costs_to_date": costs_to.isoformat(),
            "payments_from_date": payments_from.isoformat(),
            "payments_to_date": payments_to.isoformat(),
            "invoices_in_costs_range": invoices_in_range,
            "invoices_in_costs_range_total": invoices_total,
            "payments_in_payments_range": payments_in_range,
            "payments_in_payments_range_total": payments_created_total,
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
        if len(parts) >= 5 and parts[1] == "api" and parts[2] == "customers" and parts[4] == "payments":
            return self._customer_payments_get(parts[3], parts, parse_qs(parsed.query))
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self):
        self._log_api_request()
        parts = urlparse(self.path).path.split("/")
        if len(parts) >= 5 and parts[1] == "api" and parts[2] == "customers" and parts[4] == "payments":
            return self._customer_payments_post(parts[3], parts)
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_PUT(self):
        self._log_api_request()
        parts = urlparse(self.path).path.split("/")
        if len(parts) >= 5 and parts[1] == "api" and parts[2] == "customers" and parts[4] == "payments":
            return self._customer_payments_put(parts[3], parts)
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length).decode("utf-8")) if length > 0 else {}

    def _customer_payments_get(self, customer_id: str, parts: list[str], query: dict[str, list[str]]):
        client = OpenSearchClient(debug=DEBUG_MODE)
        partition = _payments_partition(customer_id)
        try:
            if len(parts) == 5:
                return self._json(client.search_customer_payments(customer_id, size=int(query.get("size", ["25"])[0])))
            if len(parts) == 7 and parts[5] == "events":
                return self._json(client.get_payment_event(partition, parts[6]))
            if len(parts) == 7 and parts[5] == "invoices":
                return self._json(client.search_customer_invoice_payments(customer_id, parts[6]))
            if len(parts) == 6 and parts[5] == "total-paid":
                return self._json(client.get_total_paid_by_customer(customer_id))
            if len(parts) == 6 and parts[5] == "balance":
                now = dt.datetime.now(dt.timezone.utc)
                raw_costs_from = query.get("costs_from_date", [None])[0]
                raw_costs_to = query.get("costs_to_date", [None])[0]
                raw_payments_from = query.get("payments_from_date", [None])[0]
                raw_payments_to = query.get("payments_to_date", [None])[0]
                raw_as_of = query.get("as_of_date", [None])[0]
                try:
                    costs_from = _parse_iso_date_or_datetime(raw_costs_from)
                    costs_to = _parse_iso_date_or_datetime(raw_costs_to, end_of_day_for_date_only=True)
                    payments_from = _parse_iso_date_or_datetime(raw_payments_from)
                    payments_to = _parse_iso_date_or_datetime(raw_payments_to, end_of_day_for_date_only=True)
                    as_of = _parse_iso_date_or_datetime(raw_as_of, end_of_day_for_date_only=True)
                except ValueError:
                    return self._json({"error": "date values must be ISO8601 date or datetime (e.g. 2026-01-01 or 2026-01-01T12:00:00Z)"}, status=400)
                try:
                    onboarding_start = _get_customer_onboarding_start(customer_id, now)
                    effective_costs_from = costs_from or (_start_of_month(as_of) if as_of else onboarding_start)
                    effective_costs_to = costs_to or as_of or _end_of_last_month(now)
                    effective_payments_from = payments_from or onboarding_start
                    effective_payments_to = payments_to or as_of or now
                    if effective_costs_from > effective_costs_to:
                        return self._json({"error": "costs_from_date must be before or equal to costs_to_date"}, status=400)
                    if effective_payments_from > effective_payments_to:
                        return self._json({"error": "payments_from_date must be before or equal to payments_to_date"}, status=400)
                    return self._json(_compute_customer_balance(customer_id, effective_costs_from, effective_costs_to, effective_payments_from, effective_payments_to))
                except CostsServiceCustomerNotFoundError as exc:
                    return self._json({"error": str(exc)}, status=404)
                except CostsServiceError as exc:
                    return self._json({"error": str(exc)}, status=502)
            if len(parts) == 6 and parts[5] == "mapping":
                return self._json(client.get_index_mapping(partition))
            if len(parts) == 6 and parts[5] == "settings":
                return self._json(client.get_index_settings(partition))
        except (OpenSearchApiError, OpenSearchError) as exc:
            return self._json({"error": str(exc), "opensearch_url": client.endpoint}, status=502)
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def _customer_payments_post(self, customer_id: str, parts: list[str]):
        client = OpenSearchClient(debug=DEBUG_MODE)
        partition = _payments_partition(customer_id)
        try:
            if len(parts) == 6 and parts[5] == "setup":
                return self._json({"template": client.create_payments_template(), "payments_index": client.create_payments_index(partition), "balances_index": client.create_balances_index()}, status=201)
            if len(parts) == 7 and parts[5] == "events" and parts[6] == "bulk":
                body = self._read_json_body()
                events = body.get("events", [])
                for event in events:
                    event["customer_id"] = customer_id
                    event.setdefault("currency", "DKK")
                return self._json(client.bulk_payment_events(events, partition), status=201)
            if len(parts) == 6 and parts[5] == "refresh":
                return self._json(client.refresh_index(partition))
        except (OpenSearchApiError, OpenSearchError) as exc:
            return self._json({"error": str(exc), "opensearch_url": client.endpoint}, status=502)
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def _customer_payments_put(self, customer_id: str, parts: list[str]):
        client = OpenSearchClient(debug=DEBUG_MODE)
        partition = _payments_partition(customer_id)
        try:
            if len(parts) == 7 and parts[5] == "events":
                body = self._read_json_body()
                body["customer_id"] = customer_id
                body.setdefault("currency", "DKK")
                body.setdefault("ingested_at", dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat())
                return self._json(client.upsert_payment_event(partition, parts[6], body), status=201)
            if len(parts) == 6 and parts[5] == "balance":
                body = self._read_json_body()
                costs_total = float(body.get("costs_total", body.get("due_total", 0)))
                payments_total = float(body.get("payments_total", body.get("paid_total", 0)))
                return self._json(
                    client.upsert_balance(
                        customer_id,
                        body.get("currency", "DKK"),
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

    costs_service_url, costs_service_defaulted = describe_env("COSTS_SERVICE_URL", default="http://localhost:8083")
    print_env_resolution("COSTS_SERVICE_URL", costs_service_url, costs_service_defaulted)
    validate_http_endpoint("COSTS_SERVICE_URL", costs_service_url, health_path="/healthz")

    document_service_url, document_service_defaulted = describe_env("DOCUMENT_GENERATOR_SERVICE_URL", default="http://localhost:8084")
    print_env_resolution("DOCUMENT_GENERATOR_SERVICE_URL", document_service_url, document_service_defaulted)
    validate_http_endpoint("DOCUMENT_GENERATOR_SERVICE_URL", document_service_url, health_path="/healthz")

    global COSTS_SERVICE_URL, DOCUMENT_GENERATOR_SERVICE_URL
    COSTS_SERVICE_URL = costs_service_url.rstrip("/")
    DOCUMENT_GENERATOR_SERVICE_URL = document_service_url.rstrip("/")

    os_verify, os_verify_defaulted = describe_env("OS_VERIFY")
    print_env_resolution("OS_VERIFY", os_verify, os_verify_defaulted)

    ThreadingHTTPServer(("0.0.0.0", args.port), PaymentsHandler).serve_forever()


if __name__ == "__main__":
    run()
