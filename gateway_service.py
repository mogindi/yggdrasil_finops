#!/usr/bin/env python3
import argparse
import http.client
import logging
import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import error, request
from urllib.parse import urlparse

from startup_validation import describe_env, print_env_resolution, validate_http_endpoint


COSTS_SERVICE_URL = os.environ.get("COSTS_SERVICE_URL")
DOCUMENT_GENERATOR_SERVICE_URL = os.environ.get("DOCUMENT_GENERATOR_SERVICE_URL")
CHECKOUT_SERVICE_URL = os.environ.get("CHECKOUT_SERVICE_URL")
PAYMENTS_SERVICE_URL = os.environ.get("PAYMENTS_SERVICE_URL")


class GatewayHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            return self._json({"status": "ok", "service": "gateway"})
        self._proxy("GET")

    def do_POST(self):
        self._proxy("POST")

    def do_PUT(self):
        self._proxy("PUT")

    def _service_url_for_path(self, path: str) -> str | None:
        if path == "/" or path.startswith("/static/"):
            return COSTS_SERVICE_URL
        if not path.startswith("/api/projects/"):
            return None

        parts = path.split("/")
        if len(parts) >= 5 and parts[4] == "costs":
            return COSTS_SERVICE_URL
        if len(parts) == 7 and parts[4] == "payments" and parts[5] == "revolut" and parts[6] == "order":
            return CHECKOUT_SERVICE_URL
        if len(parts) >= 5 and parts[4] in {"invoices", "receipts"}:
            return DOCUMENT_GENERATOR_SERVICE_URL
        if len(parts) >= 5 and parts[4] == "payments":
            return PAYMENTS_SERVICE_URL
        return None

    def _proxy(self, method: str):
        parsed = urlparse(self.path)
        base = self._service_url_for_path(parsed.path)
        if not base:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return

        target = f"{base}{self.path}"
        content_length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(content_length) if content_length > 0 else None

        outbound_headers = {k: v for k, v in self.headers.items() if k.lower() != "host"}
        req = request.Request(target, data=body, headers=outbound_headers, method=method)
        try:
            with request.urlopen(req) as upstream:
                payload = upstream.read()
                self.send_response(upstream.status)
                for key, value in upstream.headers.items():
                    if key.lower() in {"transfer-encoding", "connection", "server", "date"}:
                        continue
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        except error.HTTPError as exc:
            payload = exc.read()
            self.send_response(exc.code)
            content_type = exc.headers.get("Content-Type", "application/json")
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (error.URLError, http.client.RemoteDisconnected, ConnectionResetError, TimeoutError) as exc:
            reason = getattr(exc, "reason", str(exc))
            self._json({"error": f"upstream unavailable: {reason}"}, status=502)

    def _json(self, payload: dict, status: int = 200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run() -> None:
    parser = argparse.ArgumentParser(description="Yggdrasil FinOps API gateway")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8082")), help="Port to bind the HTTP server to")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    if args.debug:
        logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    for var_name, health_path in [
        ("COSTS_SERVICE_URL", "/healthz"),
        ("DOCUMENT_GENERATOR_SERVICE_URL", "/healthz"),
        ("CHECKOUT_SERVICE_URL", "/healthz"),
        ("PAYMENTS_SERVICE_URL", "/healthz"),
    ]:
        value, using_default = describe_env(var_name)
        print_env_resolution(var_name, value, using_default)
        validate_http_endpoint(var_name, value, health_path=health_path)

    global COSTS_SERVICE_URL, DOCUMENT_GENERATOR_SERVICE_URL, CHECKOUT_SERVICE_URL, PAYMENTS_SERVICE_URL
    COSTS_SERVICE_URL = os.environ["COSTS_SERVICE_URL"]
    DOCUMENT_GENERATOR_SERVICE_URL = os.environ["DOCUMENT_GENERATOR_SERVICE_URL"]
    CHECKOUT_SERVICE_URL = os.environ["CHECKOUT_SERVICE_URL"]
    PAYMENTS_SERVICE_URL = os.environ["PAYMENTS_SERVICE_URL"]

    server = ThreadingHTTPServer(("0.0.0.0", args.port), GatewayHandler)
    print(f"Gateway listening on http://0.0.0.0:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    run()
