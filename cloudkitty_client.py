import datetime as dt
import json
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


class CloudKittyClient:
    def __init__(self) -> None:
        self.auth_url = os.environ.get("OS_AUTH_URL", "").rstrip("/")
        if not self.auth_url:
            raise OpenStackAuthError("OS_AUTH_URL is required")
        self.username = os.environ.get("OS_USERNAME")
        self.password = os.environ.get("OS_PASSWORD")
        self.user_domain = os.environ.get("OS_USER_DOMAIN_NAME", "Default")
        self.project_domain = os.environ.get("OS_PROJECT_DOMAIN_NAME", "Default")
        self.project_name = os.environ.get("OS_PROJECT_NAME")
        self.project_id = os.environ.get("OS_PROJECT_ID")
        self.region_name = os.environ.get("OS_REGION_NAME")
        self.interface = os.environ.get("OS_INTERFACE", "public")
        self.verify = os.environ.get("OS_VERIFY", "true").lower() not in {"0", "false", "no"}

        self._token = ""
        self._cloudkitty_endpoint = os.environ.get("CLOUDKITTY_ENDPOINT", "")
        self._ssl_ctx = ssl.create_default_context() if self.verify else ssl._create_unverified_context()

    def _http_json(self, method: str, url: str, headers: dict[str, str] | None = None, params: dict[str, Any] | None = None, body: dict[str, Any] | None = None) -> tuple[int, dict[str, str], dict[str, Any]]:
        if params:
            query = parse.urlencode({k: v for k, v in params.items() if v is not None})
            url = f"{url}?{query}"
        payload = None if body is None else json.dumps(body).encode("utf-8")
        req = request.Request(url, data=payload, method=method)
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with request.urlopen(req, context=self._ssl_ctx, timeout=60) as resp:
                raw = resp.read().decode("utf-8").strip()
                return resp.status, dict(resp.headers), json.loads(raw) if raw else {}
        except HTTPError as exc:
            raw = exc.read().decode("utf-8")
            raise CloudKittyError(f"HTTP {exc.code} calling {url}: {raw}") from exc
        except URLError as exc:
            raise CloudKittyError(f"Failed calling {url}: {exc}") from exc

    def authenticate(self) -> None:
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
        status, headers, token_body = self._http_json("POST", f"{self.auth_url}/auth/tokens", body=payload)
        if status not in (200, 201):
            raise OpenStackAuthError(f"Keystone auth failed with {status}")
        token = headers.get("X-Subject-Token") or headers.get("x-subject-token")
        if not token:
            raise OpenStackAuthError("No token in Keystone response")
        self._token = token
        if not self._cloudkitty_endpoint:
            self._cloudkitty_endpoint = self._find_cloudkitty_endpoint(token_body)

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
            self.authenticate()
        _, _, payload = self._http_json(method, f"{self.endpoint}{path}", headers={"X-Auth-Token": self._token}, params=params, body=body)
        return payload

    @staticmethod
    def _sum_cost_values(node: Any) -> Decimal:
        total = Decimal("0")
        if isinstance(node, dict):
            for key, value in node.items():
                if key.lower() in {"cost", "total", "price", "rated_cost"}:
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
        begin = (now - dt.timedelta(hours=24)).replace(microsecond=0).isoformat()
        end = now.replace(microsecond=0).isoformat()
        for path, params in [
            ("/v1/summary", {"tenant_id": project_id, "begin": begin, "end": end}),
            ("/v1/report/summary", {"tenant_id": project_id, "begin": begin, "end": end}),
            ("/v2/summary", {"project_id": project_id, "begin": begin, "end": end}),
        ]:
            try:
                payload = self.request("GET", path, params=params)
                value = self._sum_cost_values(payload)
                if value != Decimal("0") or payload:
                    return float(value)
            except CloudKittyError:
                continue
        raise CloudKittyError("Unable to compute aggregate cost")

    def get_project_time_series(self, project_id: str, start: dt.datetime, end: dt.datetime, resolution: str = "day") -> list[dict[str, Any]]:
        start_iso = start.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()
        end_iso = end.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()
        for path, params in [
            ("/v1/summary", {"tenant_id": project_id, "begin": start_iso, "end": end_iso, "groupby": resolution}),
            ("/v1/report/summary", {"tenant_id": project_id, "begin": start_iso, "end": end_iso, "groupby": resolution}),
            ("/v2/summary", {"project_id": project_id, "begin": start_iso, "end": end_iso, "groupby": resolution}),
        ]:
            try:
                series = self._extract_series(self.request("GET", path, params=params))
                if series:
                    return series
            except CloudKittyError:
                continue
        raise CloudKittyError("Unable to fetch time series data")

    def _extract_series(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        series: list[dict[str, Any]] = []

        def visit(node: Any) -> None:
            if isinstance(node, dict):
                if "begin" in node and "cost" in node:
                    series.append({"timestamp": node["begin"], "cost": float(node["cost"])})
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
        defaults = {
            "instance": [{"value": "small", "cost": 0.03}, {"value": "medium", "cost": 0.07}, {"value": "large", "cost": 0.12}],
            "volume": [{"value": "standard", "cost": 0.10}, {"value": "ssd", "cost": 0.18}],
            "network.bw.out": [{"value": "default", "cost": 0.02}],
        }
        summary = {"services": []}
        for service_name, mappings in defaults.items():
            service = self._get_or_create_service(service_name)
            field = self._get_or_create_field(service["service_id"], "flavor")
            self._ensure_mappings(field["field_id"], mappings)
            summary["services"].append({"service": service_name, "service_id": service["service_id"], "field_id": field["field_id"], "mappings": mappings})
        return summary

    def _get_or_create_service(self, name: str) -> dict[str, Any]:
        for service in self.request("GET", "/v1/rating/hashmap/services").get("services", []):
            if service.get("name") == name:
                return service
        return self.request("POST", "/v1/rating/hashmap/services", body={"name": name})

    def _get_or_create_field(self, service_id: str, field_name: str) -> dict[str, Any]:
        for field in self.request("GET", f"/v1/rating/hashmap/services/{service_id}/fields").get("fields", []):
            if field.get("name") == field_name:
                return field
        return self.request("POST", f"/v1/rating/hashmap/services/{service_id}/fields", body={"name": field_name})

    def _ensure_mappings(self, field_id: str, mappings: list[dict[str, Any]]) -> None:
        existing = self.request("GET", f"/v1/rating/hashmap/fields/{field_id}/mappings")
        existing_values = {m.get("value") for m in existing.get("mappings", [])}
        for mapping in mappings:
            if mapping["value"] in existing_values:
                continue
            self.request("POST", f"/v1/rating/hashmap/fields/{field_id}/mappings", body=mapping)
