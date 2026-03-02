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


DEBUG_MODE = False


def _payments_partition(project_id: str) -> str:
    return f"project:{project_id}"


class PaymentsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            return self._json({"status": "ok", "service": "payments"})
        parts = parsed.path.split("/")
        if len(parts) >= 5 and parts[1] == "api" and parts[2] == "projects" and parts[4] == "payments":
            return self._project_payments_get(parts[3], parts, parse_qs(parsed.query))
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self):
        parts = urlparse(self.path).path.split("/")
        if len(parts) >= 5 and parts[1] == "api" and parts[2] == "projects" and parts[4] == "payments":
            return self._project_payments_post(parts[3], parts)
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_PUT(self):
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
                return self._json(client.get_balance(project_id))
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
                return self._json(client.upsert_balance(project_id, body.get("currency", "USD"), float(body.get("paid_total", 0)), float(body.get("due_total", 0))), status=201)
        except (OpenSearchApiError, OpenSearchError) as exc:
            return self._json({"error": str(exc), "opensearch_url": client.endpoint}, status=502)
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def _json(self, payload: dict, status: int = 200):
        body = json.dumps(payload).encode("utf-8")
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
    DEBUG_MODE = args.debug
    if DEBUG_MODE:
        logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ThreadingHTTPServer(("0.0.0.0", args.port), PaymentsHandler).serve_forever()


if __name__ == "__main__":
    run()
