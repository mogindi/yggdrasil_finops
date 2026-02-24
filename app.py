#!/usr/bin/env python3
import datetime as dt
import json
import os
import argparse
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from cloudkitty_client import CloudKittyClient, CloudKittyError, OpenStackAuthError, ProjectNotFoundError
from opensearch_client import OpenSearchApiError, OpenSearchClient, OpenSearchError


ROOT = Path(__file__).resolve().parent
DEBUG_MODE = False


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


def _default_year_month() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")


class CostHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            return self._serve_file(ROOT / "templates" / "index.html", "text/html")
        if parsed.path == "/healthz":
            return self._json({"status": "ok"})
        if parsed.path.startswith("/static/"):
            return self._serve_file(ROOT / parsed.path.lstrip("/"), self._content_type(parsed.path))
        if parsed.path.startswith("/api/projects/"):
            parts = parsed.path.split("/")
            if len(parts) == 5 and parts[1] == "api" and parts[2] == "projects" and parts[4] == "costs":
                project_id = parts[3]
                return self._project_costs(project_id, parse_qs(parsed.query))
            if len(parts) == 6 and parts[1] == "api" and parts[2] == "projects" and parts[4] == "costs":
                project_id = parts[3]
                if parts[5] == "last-month":
                    return self._project_costs_last_month(project_id, parse_qs(parsed.query))
                if parts[5] == "monthly":
                    return self._project_costs_monthly(project_id)
                return self._project_costs_for_month(project_id, parts[5], parse_qs(parsed.query))

            if len(parts) >= 5 and parts[1] == "api" and parts[2] == "projects" and parts[4] == "payments":
                project_id = parts[3]
                return self._project_payments_get(project_id, parts, parse_qs(parsed.query))

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self):
        parsed = urlparse(self.path)
        parts = parsed.path.split("/")
        if len(parts) >= 5 and parts[1] == "api" and parts[2] == "projects" and parts[4] == "payments":
            project_id = parts[3]
            return self._project_payments_post(project_id, parts, parse_qs(parsed.query))
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_PUT(self):
        parsed = urlparse(self.path)
        parts = parsed.path.split("/")
        if len(parts) >= 5 and parts[1] == "api" and parts[2] == "projects" and parts[4] == "payments":
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

    def _project_payments_get(self, project_id: str, parts: list[str], query: dict[str, list[str]]):
        if not self._ensure_cloudkitty_project_exists(project_id):
            return
        client = OpenSearchClient(debug=DEBUG_MODE)
        year_month = query.get("month", [_default_year_month()])[0]
        try:
            if len(parts) == 5:
                size = int(query.get("size", ["25"])[0])
                return self._json(client.search_project_payments(project_id, size=size))
            if len(parts) == 7 and parts[5] == "events":
                return self._json(client.get_payment_event(year_month, parts[6]))
            if len(parts) == 7 and parts[5] == "invoices":
                return self._json(client.search_project_invoice_payments(project_id, parts[6]))
            if len(parts) == 6 and parts[5] == "total-paid":
                return self._json(client.get_total_paid(project_id))
            if len(parts) == 6 and parts[5] == "balance":
                return self._json(client.get_balance(project_id))
            if len(parts) == 6 and parts[5] == "mapping":
                return self._json(client.get_index_mapping(year_month))
            if len(parts) == 6 and parts[5] == "settings":
                return self._json(client.get_index_settings(year_month))
        except (OpenSearchApiError, OpenSearchError) as exc:
            return self._json({"error": str(exc), "opensearch_url": client.endpoint}, status=502)

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def _project_payments_post(self, project_id: str, parts: list[str], query: dict[str, list[str]]):
        if not self._ensure_cloudkitty_project_exists(project_id):
            return
        client = OpenSearchClient(debug=DEBUG_MODE)
        year_month = query.get("month", [_default_year_month()])[0]
        try:
            if len(parts) == 6 and parts[5] == "setup":
                payload = {
                    "template": client.create_payments_template(),
                    "payments_index": client.create_payments_index(year_month),
                    "balances_index": client.create_balances_index(),
                }
                return self._json(payload, status=201)
            if len(parts) == 7 and parts[5] == "events" and parts[6] == "bulk":
                body = self._read_json_body()
                events = body.get("events", [])
                for event in events:
                    event["project_id"] = project_id
                return self._json(client.bulk_payment_events(events, year_month), status=201)
            if len(parts) == 6 and parts[5] == "refresh":
                return self._json(client.refresh_index(year_month))
        except (OpenSearchApiError, OpenSearchError) as exc:
            return self._json({"error": str(exc), "opensearch_url": client.endpoint}, status=502)

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def _project_payments_put(self, project_id: str, parts: list[str], query: dict[str, list[str]]):
        if not self._ensure_cloudkitty_project_exists(project_id):
            return
        client = OpenSearchClient(debug=DEBUG_MODE)
        year_month = query.get("month", [_default_year_month()])[0]
        try:
            if len(parts) == 7 and parts[5] == "events":
                body = self._read_json_body()
                body["project_id"] = project_id
                body.setdefault("ingested_at", dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat())
                return self._json(client.upsert_payment_event(year_month, parts[6], body), status=201)
            if len(parts) == 6 and parts[5] == "balance":
                body = self._read_json_body()
                return self._json(
                    client.upsert_balance(
                        project_id,
                        body.get("currency", "USD"),
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
                "currency": os.environ.get("CLOUDKITTY_CURRENCY", "USD"),
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
        current_month_start = _start_of_current_month_utc(now)
        end = current_month_start - dt.timedelta(seconds=1)
        start = dt.datetime(1970, 1, 1, 0, 0, 0, tzinfo=dt.timezone.utc)

        client = CloudKittyClient(debug=DEBUG_MODE)
        try:
            client.ensure_project_exists(project_id)
            series = client.get_project_time_series(project_id, start, end, "month")
        except ProjectNotFoundError as exc:
            return self._json({"error": str(exc)}, status=404)
        except (OpenStackAuthError, CloudKittyError) as exc:
            return self._json({"error": str(exc)}, status=502)

        monthly_series = [point for point in series if _parse_date(point["timestamp"], end) < current_month_start]
        aggregate = sum(point["cost"] for point in monthly_series)

        return self._json(
            {
                "project_id": project_id,
                "aggregate_cost_now": aggregate,
                "currency": os.environ.get("CLOUDKITTY_CURRENCY", "USD"),
                "time_series": monthly_series,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "resolution": "month",
            }
        )

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

    port = args.port
    server = ThreadingHTTPServer(("0.0.0.0", port), CostHandler)
    mode = "debug" if DEBUG_MODE else "normal"
    print(f"Serving on http://0.0.0.0:{port} ({mode} mode)")
    server.serve_forever()


if __name__ == "__main__":
    run()
