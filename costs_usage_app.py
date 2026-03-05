#!/usr/bin/env python3
import argparse
import logging
import datetime as dt
import html
import json
import os
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from cloudkitty_client import CloudKittyClient, CloudKittyError, OpenStackAuthError, ProjectNotFoundError
from currency import get_default_currency
from startup_validation import describe_env, env_flag_enabled, print_env_resolution

ROOT = Path(__file__).resolve().parent
DEBUG_MODE = False
LOGGER = logging.getLogger("costs_usage_app")


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
    next_month_start = dt.datetime(year + (month == 12), 1 if month == 12 else month + 1, 1, tzinfo=dt.timezone.utc)
    return start, next_month_start - dt.timedelta(seconds=1)


def _start_of_month_utc(moment: dt.datetime) -> dt.datetime:
    return moment.astimezone(dt.timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


class CostsUsageHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

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
        if parsed.path == "/":
            return self._serve_file(ROOT / "templates" / "index.html", "text/html")
        if parsed.path == "/healthz":
            return self._json({"status": "ok", "service": "costs_usage"})
        if parsed.path.startswith("/static/"):
            return self._serve_file(ROOT / parsed.path.lstrip("/"), self._content_type(parsed.path))

        if parsed.path.startswith("/api/projects/"):
            parts = parsed.path.split("/")
            if len(parts) == 5 and parts[4] == "costs":
                return self._project_costs(parts[3], parse_qs(parsed.query))
            if len(parts) == 6 and parts[4] == "costs":
                if parts[5] == "last-month":
                    return self._project_costs_last_month(parts[3], parse_qs(parsed.query))
                if parts[5] == "monthly":
                    return self._project_costs_monthly(parts[3])
                return self._project_costs_for_month(parts[3], parts[5], parse_qs(parsed.query))
            if len(parts) == 7 and parts[4] == "costs" and parts[5] == "monthly" and parts[6] == "graph":
                return self._project_costs_monthly_graph(parts[3])

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

        return self._json({
            "project_id": project_id,
            "aggregate_cost_now": aggregate,
            "currency": get_default_currency(),
            "time_series": series,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "resolution": resolution,
        })

    def _project_costs_last_month(self, project_id: str, query: dict[str, list[str]]):
        start, end = _last_month_bounds(dt.datetime.now(dt.timezone.utc))
        query_with_range = dict(query)
        query_with_range["start"] = [start.isoformat()]
        query_with_range["end"] = [end.isoformat()]
        return self._project_costs(project_id, query_with_range)

    def _project_costs_for_month(self, project_id: str, yyyy_mm: str, query: dict[str, list[str]]):
        try:
            year_s, month_s = yyyy_mm.split("-", 1)
            start, end = _month_bounds_utc(int(year_s), int(month_s))
        except Exception:
            return self._json({"error": "month must be YYYY-MM"}, status=400)
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
            series = client.get_project_time_series(project_id, start, now, "month")
        except ProjectNotFoundError as exc:
            return self._json({"error": str(exc)}, status=404)
        except (OpenStackAuthError, CloudKittyError) as exc:
            return self._json({"error": str(exc)}, status=502)
        monthly_series = [p for p in series if _parse_date(p["timestamp"], now) >= start]
        return self._json({"project_id": project_id, "aggregate_cost_now": sum(p["cost"] for p in monthly_series), "currency": get_default_currency(), "time_series": monthly_series, "start": start.isoformat(), "end": now.isoformat(), "resolution": "month"})

    def _project_costs_monthly_graph(self, project_id: str):
        return self._html(f"<html><body><h1>Monthly graph for {html.escape(project_id)}</h1><p>Use /costs/monthly for raw series.</p></body></html>")

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
        body = payload.encode("utf-8")
        if self.path.startswith("/api/"):
            LOGGER.debug(
                "API response: method=%s path=%s status=%s content_type=text/html payload_bytes=%s",
                self.command,
                self.path,
                HTTPStatus.OK,
                len(body),
            )
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
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
    parser = argparse.ArgumentParser(description="Costs usage service")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    global DEBUG_MODE
    DEBUG_MODE = args.debug or env_flag_enabled("DEBUG", default=False)
    if DEBUG_MODE:
        logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    for var_name, default in [
        ("OS_AUTH_URL", None),
        ("OS_USERNAME", None),
        ("OS_PASSWORD", None),
        ("OS_USER_DOMAIN_NAME", None),
        ("OS_PROJECT_DOMAIN_NAME", None),
        ("OS_INTERFACE", None),
        ("CLOUDKITTY_CURRENCY", None),
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

    server = ThreadingHTTPServer(("0.0.0.0", args.port), CostsUsageHandler)
    server.serve_forever()


if __name__ == "__main__":
    run()
