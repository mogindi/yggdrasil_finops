import os
import time
from urllib import error, parse, request


class StartupValidationError(RuntimeError):
    pass


def describe_env(var_name: str, default: str | None = None) -> tuple[str, bool]:
    raw = os.environ.get(var_name)
    if raw is not None and raw.strip() != "":
        return raw.strip(), False
    if default is None:
        raise StartupValidationError(f"{var_name} is required")
    return default, True


def ensure_http_url(var_name: str, value: str) -> None:
    parsed = parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise StartupValidationError(f"{var_name} must be a valid http(s) URL, got: {value!r}")


def validate_http_endpoint(var_name: str, value: str, *, health_path: str = "/healthz", retries: int = 5, delay_seconds: float = 1.0) -> None:
    ensure_http_url(var_name, value)
    target = value.rstrip("/") + health_path
    last_error = "unknown error"
    for _ in range(retries):
        req = request.Request(target, headers={"Accept": "application/json"})
        try:
            with request.urlopen(req, timeout=5):
                return
        except error.HTTPError as exc:
            if 200 <= exc.code < 500:
                return
            last_error = f"HTTP {exc.code}"
        except error.URLError as exc:
            last_error = str(exc.reason)
        time.sleep(delay_seconds)
    raise StartupValidationError(f"{var_name} endpoint is not reachable at {target}: {last_error}")


def print_env_resolution(var_name: str, value: str, using_default: bool) -> None:
    source = "default" if using_default else "environment"
    print(f"[startup] {var_name}={value} ({source})")
