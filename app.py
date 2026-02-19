#!/usr/bin/env python3
import datetime as dt
import json
import os
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from cloudkitty_client import CloudKittyClient, CloudKittyError, OpenStackAuthError


ROOT = Path(__file__).resolve().parent
STATIC_ROOT = (ROOT / "static").resolve()


def _parse_date(raw: str | None, default: dt.datetime) -> dt.datetime:
    if not raw:
        return default
    return dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))


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
            safe_static_path = self._resolve_static_path(parsed.path)
            if safe_static_path is None:
                self.send_error(HTTPStatus.FORBIDDEN, "Forbidden")
                return
            return self._serve_file(safe_static_path, self._content_type(parsed.path))
        if parsed.path.startswith("/api/projects/") and parsed.path.endswith("/costs"):
            parts = parsed.path.split("/")
            if len(parts) >= 5:
                project_id = parts[3]
                return self._project_costs(project_id, parse_qs(parsed.query))
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def _project_costs(self, project_id: str, query: dict[str, list[str]]):
        now = dt.datetime.now(dt.timezone.utc)
        start = _parse_date(query.get("start", [None])[0], now - dt.timedelta(days=30))
        end = _parse_date(query.get("end", [None])[0], now)
        resolution = query.get("resolution", ["day"])[0]
        include_series = query.get("include_series", ["true"])[0].lower() != "false"

        client = CloudKittyClient()
        try:
            aggregate = client.get_project_aggregate_now(project_id)
            series = client.get_project_time_series(project_id, start, end, resolution) if include_series else []
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
    def _resolve_static_path(request_path: str) -> Path | None:
        relative = unquote(request_path.removeprefix("/static/"))
        candidate = (STATIC_ROOT / relative).resolve()
        try:
            candidate.relative_to(STATIC_ROOT)
        except ValueError:
            return None
        return candidate

    @staticmethod
    def _content_type(path: str) -> str:
        if path.endswith(".js"):
            return "application/javascript"
        if path.endswith(".css"):
            return "text/css"
        return "text/plain"


def run() -> None:
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), CostHandler)
    print(f"Serving on http://0.0.0.0:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run()
