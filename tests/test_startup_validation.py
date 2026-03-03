import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from startup_validation import StartupValidationError, describe_env, ensure_http_url, validate_http_endpoint


class _OkHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


class StartupValidationTests(unittest.TestCase):
    def test_describe_env_returns_default_when_unset(self):
        with patch.dict("os.environ", {}, clear=True):
            value, using_default = describe_env("OPENSEARCH_URL", "http://localhost:9200")
        self.assertEqual(value, "http://localhost:9200")
        self.assertTrue(using_default)

    def test_describe_env_raises_when_required_missing(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(StartupValidationError):
                describe_env("REVOLUT_API_KEY", None)

    def test_ensure_http_url_rejects_invalid_scheme(self):
        with self.assertRaises(StartupValidationError):
            ensure_http_url("OPENSEARCH_URL", "ftp://example.com")

    def test_validate_http_endpoint_reaches_local_server(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _OkHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            validate_http_endpoint("LOCAL_URL", f"http://127.0.0.1:{server.server_port}", health_path="/")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
