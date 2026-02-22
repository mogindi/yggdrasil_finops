import datetime as dt
import json
import logging
import os
import ssl
from decimal import Decimal
from typing import Any
from urllib import parse, request
from urllib.error import HTTPError, URLError


class OpenStackAuthError(RuntimeError):
    pass


class CloudKittyError(RuntimeError):
    pass


class CloudKittyApiError(CloudKittyError):
    def __init__(self, message: str, status_code: int | None = None, url: str | None = None, body: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.url = url
        self.body = body


class ProjectNotFoundError(CloudKittyError):
    pass


class CloudKittyClient:
    def __init__(self, debug: bool = False) -> None:
        self.auth_url = os.environ.get("OS_AUTH_URL", "").rstrip("/")
        if not self.auth_url:
            raise OpenStackAuthError("OS_AUTH_URL is required")
        self._token_url = self._build_keystone_tokens_url(self.auth_url)
        self.username = os.environ.get("OS_USERNAME")
        self.password = os.environ.get("OS_PASSWORD")
        self.user_domain = os.environ.get("OS_USER_DOMAIN_NAME", "Default")
        self.project_domain = os.environ.get("OS_PROJECT_DOMAIN_NAME", "Default")
        self.project_name = os.environ.get("OS_PROJECT_NAME")
        self.project_id = os.environ.get("OS_PROJECT_ID")
        self.region_name = os.environ.get("OS_REGION_NAME")
        self.interface = os.environ.get("OS_INTERFACE", "public")
        self.verify = os.environ.get("OS_VERIFY", "true").lower() not in {"0", "false", "no"}
        self.debug = debug

        self._logger = logging.getLogger(self.__class__.__name__)
        if self.debug:
            logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
            self._logger.debug("Debug logging enabled")

        self._token = ""
        self._cloudkitty_endpoint = os.environ.get("CLOUDKITTY_ENDPOINT", "")
        self._ssl_ctx = ssl.create_default_context() if self.verify else ssl._create_unverified_context()

    @staticmethod
    def _build_keystone_tokens_url(auth_url: str) -> str:
        parsed = parse.urlparse(auth_url)
        path = (parsed.path or "").rstrip("/")
        if path.endswith("/auth/tokens"):
            return parse.urlunparse(parsed)
        if not path:
            path = "/v3"
        elif not path.endswith("/v3"):
            path = f"{path}/v3"
        return parse.urlunparse(parsed._replace(path=f"{path}/auth/tokens"))

    def _debug(self, message: str) -> None:
        if self.debug:
            self._logger.debug(message)

    def _safe_body(self, body: dict[str, Any] | None) -> dict[str, Any] | None:
        if not body:
            return body
        body_text = json.dumps(body)
        if self.password:
            body_text = body_text.replace(self.password, "***")
        return json.loads(body_text)

    def _http_json(self, method: str, url: str, headers: dict[str, str] | None = None, params: dict[str, Any] | None = None, body: dict[str, Any] | None = None) -> tuple[int, dict[str, str], dict[str, Any]]:
        if params:
            query = parse.urlencode({k: v for k, v in params.items() if v is not None})
            url = f"{url}?{query}"
        safe_headers = dict(headers or {})
        if safe_headers.get("X-Auth-Token"):
            safe_headers["X-Auth-Token"] = "***"
        self._debug(f"HTTP request: method={method} url={url} headers={safe_headers} params={params} body={self._safe_body(body)}")
        payload = None if body is None else json.dumps(body).encode("utf-8")
        req = request.Request(url, data=payload, method=method)
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with request.urlopen(req, context=self._ssl_ctx, timeout=60) as resp:
                raw = resp.read().decode("utf-8").strip()
                payload_data = json.loads(raw) if raw else {}
                self._debug(f"HTTP response: status={resp.status} url={url} body={payload_data}")
                return resp.status, dict(resp.headers), payload_data
        except HTTPError as exc:
            raw = exc.read().decode("utf-8")
            self._debug(f"HTTP error: status={exc.code} url={url} body={raw}")
            raise CloudKittyApiError(f"HTTP {exc.code} calling {url}: {raw}", status_code=exc.code, url=url, body=raw) from exc
        except URLError as exc:
            self._debug(f"URL error: url={url} error={exc}")
            raise CloudKittyError(f"Failed calling {url}: {exc}") from exc

    def _build_keystone_project_url(self, project_id: str) -> str:
        parsed = parse.urlparse(self._token_url)
        path = parsed.path
        if path.endswith("/auth/tokens"):
            path = path[: -len("/auth/tokens")]
        return parse.urlunparse(parsed._replace(path=f"{path}/projects/{parse.quote(project_id)}"))

    def authenticate(self) -> None:
        self._debug("Starting authentication")
        if not self.username or not self.password:
            raise OpenStackAuthError("OS_USERNAME and OS_PASSWORD are required")
        if self.project_id:
            scope_project = {"id": self.project_id}
        elif self.project_name:
            scope_project = {"name": self.project_name, "domain": {"name": self.project_domain}}
        else:
            raise OpenStackAuthError("Set OS_PROJECT_ID or OS_PROJECT_NAME")

        payload = {
            "auth": {
                "identity": {
                    "methods": ["password"],
                    "password": {
                        "user": {
                            "name": self.username,
                            "domain": {"name": self.user_domain},
                            "password": self.password,
                        }
                    },
                },
                "scope": {"project": scope_project},
            }
        }
        self._debug(f"Authenticating against Keystone tokens URL={self._token_url}")
        status, headers, token_body = self._http_json("POST", self._token_url, body=payload)
        if status not in (200, 201):
            raise OpenStackAuthError(f"Keystone auth failed with {status}")
        token = headers.get("X-Subject-Token") or headers.get("x-subject-token")
        if not token:
            raise OpenStackAuthError("No token in Keystone response")
        self._token = token
        if not self._cloudkitty_endpoint:
            self._cloudkitty_endpoint = self._find_cloudkitty_endpoint(token_body)
        self._debug(f"Authentication successful; endpoint={self._cloudkitty_endpoint}")

    def _find_cloudkitty_endpoint(self, token_body: dict[str, Any]) -> str:
        for service in token_body.get("token", {}).get("catalog", []):
            if service.get("type") != "rating":
                continue
            for endpoint in service.get("endpoints", []):
                if self.region_name and endpoint.get("region") != self.region_name:
                    continue
                if endpoint.get("interface") == self.interface:
                    return endpoint.get("url", "").rstrip("/")
        raise OpenStackAuthError("Could not find CloudKitty endpoint; set CLOUDKITTY_ENDPOINT")

    @property
    def endpoint(self) -> str:
        if not self._cloudkitty_endpoint:
            raise OpenStackAuthError("Not authenticated")
        return self._cloudkitty_endpoint

    def request(self, method: str, path: str, params: dict[str, Any] | None = None, body: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self._token:
            self._debug("No cached auth token; authenticating before request")
            self.authenticate()
        self._debug(f"CloudKitty API call: method={method} path={path}")
        _, _, payload = self._http_json(method, f"{self.endpoint}{path}", headers={"X-Auth-Token": self._token}, params=params, body=body)
        return payload

    def ensure_project_exists(self, project_id: str) -> None:
        if not self._token:
            self.authenticate()
        project_url = self._build_keystone_project_url(project_id)
        try:
            self._http_json("GET", project_url, headers={"X-Auth-Token": self._token})
        except CloudKittyApiError as exc:
            if exc.status_code == 404:
                raise ProjectNotFoundError(f"Project '{project_id}' does not exist") from exc
            raise CloudKittyError(f"Unable to verify project '{project_id}' existence") from exc

    @staticmethod
    def _sum_cost_values(node: Any) -> Decimal:
        total = Decimal("0")
        if isinstance(node, dict):
            for key, value in node.items():
                if key.lower() in {"cost", "total", "price", "rated_cost", "rate"}:
                    try:
                        total += Decimal(str(value))
                        continue
                    except Exception:
                        pass
                total += CloudKittyClient._sum_cost_values(value)
        elif isinstance(node, list):
            for item in node:
                total += CloudKittyClient._sum_cost_values(item)
        return total

    def get_project_aggregate_now(self, project_id: str) -> float:
        now = dt.datetime.now(dt.timezone.utc)
        begin = now - dt.timedelta(hours=24)
        return self.get_project_aggregate_for_range(project_id, begin, now)

    def get_project_aggregate_for_range(self, project_id: str, start: dt.datetime, end: dt.datetime) -> float:
        self._debug(f"Fetching aggregate cost for project_id={project_id} start={start} end={end}")
        begin = start.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()
        end_iso = end.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()
        path = "/v1/report/summary"
        params = {"tenant_id": project_id, "begin": begin, "end": end_iso}
        self._debug(f"Trying aggregate endpoint path={path}")
        payload = self.request("GET", path, params=params)
        value = self._sum_cost_values(payload)
        self._debug(f"Aggregate endpoint path={path} returned value={value}")
        return float(value)

    def get_project_time_series(self, project_id: str, start: dt.datetime, end: dt.datetime, resolution: str = "day") -> list[dict[str, Any]]:
        self._debug(f"Fetching time series project_id={project_id} start={start} end={end} resolution={resolution}")
        start_iso = start.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()
        end_iso = end.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()
        path = "/v1/report/summary"
        params = {"tenant_id": project_id, "begin": start_iso, "end": end_iso, "groupby": resolution}
        self._debug(f"Trying time-series endpoint path={path}")
        series = self._extract_series(self.request("GET", path, params=params))
        self._debug(f"Time-series endpoint path={path} returned {len(series)} points")
        return series

    def _extract_series(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        series: list[dict[str, Any]] = []

        def visit(node: Any) -> None:
            if isinstance(node, dict):
                if "begin" in node and "cost" in node:
                    series.append({"timestamp": node["begin"], "cost": float(node["cost"])})
                if "begin" in node and "rate" in node:
                    series.append({"timestamp": node["begin"], "cost": float(node["rate"])})
                if "period_begin" in node and "rated_cost" in node:
                    series.append({"timestamp": node["period_begin"], "cost": float(node["rated_cost"])})
                for value in node.values():
                    visit(value)
            elif isinstance(node, list):
                for item in node:
                    visit(item)

        visit(payload)
        series.sort(key=lambda x: x["timestamp"])
        return series

    def ensure_default_hashmap_pricing(self) -> dict[str, Any]:
        self._debug("Ensuring default hashmap pricing")
        defaults = {
            "instance": [{"value": "small", "cost": 0.03}, {"value": "medium", "cost": 0.07}, {"value": "large", "cost": 0.12}],
            "volume": [{"value": "standard", "cost": 0.10}, {"value": "ssd", "cost": 0.18}],
            "network.bw.out": [{"value": "default", "cost": 0.02}],
        }
        summary = {"services": []}
        for service_name, mappings in defaults.items():
            self._debug(f"Ensuring service mappings for service={service_name}")
            service = self._get_or_create_service(service_name)
            field = self._get_or_create_field(service["service_id"], "flavor")
            self._ensure_mappings(field["field_id"], mappings)
            summary["services"].append({"service": service_name, "service_id": service["service_id"], "field_id": field["field_id"], "mappings": mappings})
        return summary

    def _get_or_create_service(self, name: str) -> dict[str, Any]:
        self._debug(f"Looking up hashmap service name={name}")
        for service in self.request("GET", "/v1/rating/hashmap/services").get("services", []):
            if service.get("name") == name:
                self._debug(f"Found existing hashmap service name={name}")
                return service
        self._debug(f"Creating hashmap service name={name}")
        return self.request("POST", "/v1/rating/hashmap/services", body={"name": name})

    def _get_or_create_field(self, service_id: str, field_name: str) -> dict[str, Any]:
        self._debug(f"Looking up hashmap field service_id={service_id} name={field_name}")
        for field in self.request("GET", f"/v1/rating/hashmap/services/{service_id}/fields").get("fields", []):
            if field.get("name") == field_name:
                self._debug(f"Found existing hashmap field service_id={service_id} name={field_name}")
                return field
        self._debug(f"Creating hashmap field service_id={service_id} name={field_name}")
        return self.request("POST", f"/v1/rating/hashmap/services/{service_id}/fields", body={"name": field_name})

    def _ensure_mappings(self, field_id: str, mappings: list[dict[str, Any]]) -> None:
        self._debug(f"Ensuring mappings for field_id={field_id}")
        existing = self.request("GET", f"/v1/rating/hashmap/fields/{field_id}/mappings")
        existing_values = {m.get("value") for m in existing.get("mappings", [])}
        for mapping in mappings:
            if mapping["value"] in existing_values:
                self._debug(f"Mapping already exists value={mapping['value']}")
                continue
            self._debug(f"Creating mapping value={mapping['value']} cost={mapping['cost']}")
            self.request("POST", f"/v1/rating/hashmap/fields/{field_id}/mappings", body=mapping)
