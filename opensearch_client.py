import datetime as dt
import json
import logging
import os
import ssl
from typing import Any
from urllib import parse, request
from urllib.error import HTTPError, URLError


class OpenSearchError(RuntimeError):
    pass


class OpenSearchApiError(OpenSearchError):
    def __init__(self, message: str, status_code: int | None = None, url: str | None = None, body: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.url = url
        self.body = body


class OpenSearchClient:
    def __init__(self, debug: bool = False) -> None:
        self.endpoint = os.environ["OPENSEARCH_URL"].rstrip("/")
        self.verify = os.environ["OS_VERIFY"].lower() not in {"0", "false", "no"}
        self.debug = debug
        self._logger = logging.getLogger(self.__class__.__name__)
        if self.debug:
            logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
            self._logger.debug("OpenSearch debug logging enabled")
        self._ssl_ctx = ssl.create_default_context() if self.verify else ssl._create_unverified_context()

    def _debug(self, message: str) -> None:
        if self.debug:
            self._logger.debug(message)

    def _http_json(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.endpoint}{path}"
        data = None
        headers = {"Content-Type": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        req = request.Request(url, method=method, data=data, headers=headers)
        body_keys = sorted(body.keys()) if isinstance(body, dict) else []
        self._debug(f"OpenSearch API call: method={method} url={url} body_keys={body_keys}")
        try:
            with request.urlopen(req, context=self._ssl_ctx, timeout=20) as resp:
                raw = resp.read().decode("utf-8")
                self._debug(f"OpenSearch API response: status={getattr(resp, 'status', 'unknown')} url={url}")
                return json.loads(raw) if raw else {}
        except HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            self._debug(f"OpenSearch API error: method={method} url={url} status={exc.code} body={error_body[:500]}")
            raise OpenSearchApiError(f"OpenSearch request failed ({exc.code})", status_code=exc.code, url=url, body=error_body) from exc
        except URLError as exc:
            self._debug(f"OpenSearch connection error: method={method} url={url} reason={exc.reason}")
            raise OpenSearchError(f"Failed to connect to OpenSearch at {self.endpoint}: {exc.reason}") from exc

    def _http_ndjson(self, path: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
        url = f"{self.endpoint}{path}"
        payload = "\n".join(json.dumps(row) for row in rows) + "\n"
        req = request.Request(url, method="POST", data=payload.encode("utf-8"), headers={"Content-Type": "application/x-ndjson"})
        self._debug(f"OpenSearch API call: method=POST url={url} ndjson_rows={len(rows)}")
        try:
            with request.urlopen(req, context=self._ssl_ctx, timeout=20) as resp:
                self._debug(f"OpenSearch API response: status={getattr(resp, 'status', 'unknown')} url={url}")
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            self._debug(f"OpenSearch bulk API error: url={url} status={exc.code} body={error_body[:500]}")
            raise OpenSearchApiError(f"OpenSearch bulk request failed ({exc.code})", status_code=exc.code, url=url, body=error_body) from exc
        except URLError as exc:
            self._debug(f"OpenSearch connection error: method=POST url={url} reason={exc.reason}")
            raise OpenSearchError(f"Failed to connect to OpenSearch at {self.endpoint}: {exc.reason}") from exc

    @staticmethod
    def _is_resource_already_exists(exc: OpenSearchApiError) -> bool:
        if exc.status_code != 400 or not exc.body:
            return False
        try:
            payload = json.loads(exc.body)
        except json.JSONDecodeError:
            return False
        err = payload.get("error", {})
        err_type = err.get("type")
        root_causes = err.get("root_cause", [])
        has_root_cause = any(cause.get("type") == "resource_already_exists_exception" for cause in root_causes)
        return err_type == "resource_already_exists_exception" or has_root_cause



    @staticmethod
    def _payments_index_name(partition: str) -> str:
        raw = partition.removeprefix("project:").strip().lower()
        normalized = "".join(ch if (ch.isalnum() or ch in "_-") else "-" for ch in raw).strip("-")
        normalized = normalized or "default"
        return f"payments-project-{normalized}"

    def create_payments_template(self) -> dict[str, Any]:
        body = {
            "index_patterns": ["payments-*"],
            "template": {
                "settings": {"number_of_shards": 3, "number_of_replicas": 1, "refresh_interval": "1s"},
                "mappings": {
                    "dynamic": "strict",
                    "properties": {
                        "event_id": {"type": "keyword"},
                        "project_id": {"type": "keyword"},
                        "invoice_id": {"type": "keyword"},
                        "payment_id": {"type": "keyword"},
                        "provider": {"type": "keyword"},
                        "currency": {"type": "keyword"},
                        "amount": {"type": "scaled_float", "scaling_factor": 100},
                        "direction": {"type": "keyword"},
                        "status": {"type": "keyword"},
                        "paid_at": {"type": "date"},
                        "ingested_at": {"type": "date"},
                        # `flattened` is not available on some OpenSearch/Elasticsearch
                        # variants used in on-prem environments. Store arbitrary metadata
                        # without indexing it to keep setup compatible.
                        "metadata": {"type": "object", "enabled": False},
                    },
                },
            },
        }
        return self._http_json("PUT", "/_index_template/payments_template", body)

    def create_payments_index(self, partition: str) -> dict[str, Any]:
        index_name = self._payments_index_name(partition)
        try:
            return self._http_json("PUT", f"/{index_name}")
        except OpenSearchApiError as exc:
            if self._is_resource_already_exists(exc):
                return {"acknowledged": True, "already_exists": True, "index": index_name}
            raise

    def create_balances_index(self) -> dict[str, Any]:
        body = {
            "settings": {"number_of_shards": 1, "number_of_replicas": 1},
            "mappings": {
                "dynamic": "strict",
                "properties": {
                    "project_id": {"type": "keyword"},
                    "currency": {"type": "keyword"},
                    "costs_total": {"type": "scaled_float", "scaling_factor": 100},
                    "payments_total": {"type": "scaled_float", "scaling_factor": 100},
                    "balance": {"type": "scaled_float", "scaling_factor": 100},
                    "paid_total": {"type": "scaled_float", "scaling_factor": 100},
                    "refunded_total": {"type": "scaled_float", "scaling_factor": 100},
                    "net_paid": {"type": "scaled_float", "scaling_factor": 100},
                    "updated_at": {"type": "date"},
                },
            },
        }
        try:
            return self._http_json("PUT", "/project-balances", body)
        except OpenSearchApiError as exc:
            if self._is_resource_already_exists(exc):
                return {"acknowledged": True, "already_exists": True, "index": "project-balances"}
            raise

    def create_billing_indexes(self) -> dict[str, Any]:
        invoice_body = {
            "settings": {"number_of_shards": 1, "number_of_replicas": 1},
            "mappings": {
                "dynamic": "strict",
                "properties": {
                    "invoice_id": {"type": "keyword"},
                    "project_id": {"type": "keyword"},
                    "customer": {
                        "properties": {
                            "name": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
                            "email": {"type": "keyword"},
                        }
                    },
                    "description": {"type": "text"},
                    "amount_due": {"type": "scaled_float", "scaling_factor": 100},
                    "amount_paid": {"type": "scaled_float", "scaling_factor": 100},
                    "currency": {"type": "keyword"},
                    "status": {"type": "keyword"},
                    "created_at": {"type": "date"},
                    "due_at": {"type": "date", "ignore_malformed": True},
                    "updated_at": {"type": "date"},
                },
            },
        }
        receipt_body = {
            "settings": {"number_of_shards": 1, "number_of_replicas": 1},
            "mappings": {
                "dynamic": "strict",
                "properties": {
                    "receipt_id": {"type": "keyword"},
                    "project_id": {"type": "keyword"},
                    "invoice_id": {"type": "keyword"},
                    "amount_paid": {"type": "scaled_float", "scaling_factor": 100},
                    "currency": {"type": "keyword"},
                    "paid_at": {"type": "date"},
                    "payment_method": {"type": "keyword"},
                    "payment_reference": {"type": "keyword"},
                    "created_at": {"type": "date"},
                },
            },
        }

        indexes: dict[str, Any] = {}
        for index_name, body in (("project-invoices", invoice_body), ("project-receipts", receipt_body)):
            try:
                indexes[index_name] = self._http_json("PUT", f"/{index_name}", body)
            except OpenSearchApiError as exc:
                if self._is_resource_already_exists(exc):
                    indexes[index_name] = {"acknowledged": True, "already_exists": True, "index": index_name}
                else:
                    raise
        return indexes

    def upsert_invoice(self, invoice_id: str, document: dict[str, Any]) -> dict[str, Any]:
        return self._http_json("PUT", f"/project-invoices/_doc/{parse.quote(invoice_id)}", document)

    def get_invoice(self, invoice_id: str) -> dict[str, Any]:
        return self._http_json("GET", f"/project-invoices/_doc/{parse.quote(invoice_id)}")

    def search_project_invoices(self, project_id: str, size: int = 200) -> dict[str, Any]:
        body = {
            "query": {"term": {"project_id": project_id}},
            "sort": [{"created_at": "desc"}],
            "size": size,
        }
        return self._http_json("GET", "/project-invoices/_search", body)

    def upsert_receipt(self, receipt_id: str, document: dict[str, Any]) -> dict[str, Any]:
        return self._http_json("PUT", f"/project-receipts/_doc/{parse.quote(receipt_id)}", document)

    def get_receipt(self, receipt_id: str) -> dict[str, Any]:
        return self._http_json("GET", f"/project-receipts/_doc/{parse.quote(receipt_id)}")

    def search_project_receipts(self, project_id: str, size: int = 200) -> dict[str, Any]:
        body = {
            "query": {"term": {"project_id": project_id}},
            "sort": [{"created_at": "desc"}],
            "size": size,
        }
        return self._http_json("GET", "/project-receipts/_search", body)

    def upsert_payment_event(self, partition: str, event_id: str, document: dict[str, Any]) -> dict[str, Any]:
        index_name = self._payments_index_name(partition)
        return self._http_json("PUT", f"/{index_name}/_doc/{parse.quote(event_id)}", document)

    def bulk_payment_events(self, events: list[dict[str, Any]], partition: str) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for event in events:
            event_id = event.get("event_id")
            rows.append({"index": {"_index": self._payments_index_name(partition), "_id": event_id}})
            rows.append(event)
        return self._http_ndjson("/_bulk", rows)

    def get_payment_event(self, partition: str, event_id: str) -> dict[str, Any]:
        index_name = self._payments_index_name(partition)
        return self._http_json("GET", f"/{index_name}/_doc/{parse.quote(event_id)}")

    def search_project_payments(self, project_id: str, size: int = 25) -> dict[str, Any]:
        body = {
            "query": {"term": {"project_id": project_id}},
            "sort": [{"paid_at": "desc"}],
            "size": size,
        }
        return self._http_json("GET", "/payments-*/_search", body)

    def search_project_invoice_payments(self, project_id: str, invoice_id: str, size: int = 100) -> dict[str, Any]:
        body = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"project_id": project_id}},
                        {"term": {"invoice_id": invoice_id}},
                        {"term": {"status": "succeeded"}},
                    ]
                }
            },
            "sort": [{"paid_at": "asc"}],
            "size": size,
        }
        return self._http_json("GET", "/payments-*/_search", body)

    def get_total_paid(self, project_id: str) -> dict[str, Any]:
        body = {
            "size": 0,
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"project_id": project_id}},
                        {"term": {"status": "succeeded"}},
                        {"term": {"direction": "in"}},
                    ]
                }
            },
            "aggs": {"total_paid": {"sum": {"field": "amount"}}},
        }
        return self._http_json("GET", "/payments-*/_search", body)

    def upsert_balance(self, project_id: str, currency: str, costs_total: float, payments_total: float) -> dict[str, Any]:
        balance = float(costs_total) - float(payments_total)
        body = {
            "doc": {
                "project_id": project_id,
                "currency": currency,
                "costs_total": float(costs_total),
                "payments_total": float(payments_total),
                "balance": balance,
                # Backward-compatible aliases for older dashboards and clients.
                "paid_total": float(payments_total),
                "refunded_total": float(costs_total),
                "net_paid": -balance,
                "updated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
            },
            "doc_as_upsert": True,
        }
        return self._http_json("POST", f"/project-balances/_update/{parse.quote(project_id)}", body)

    def get_balance(self, project_id: str) -> dict[str, Any]:
        return self._http_json("GET", f"/project-balances/_doc/{parse.quote(project_id)}")

    def get_index_mapping(self, partition: str) -> dict[str, Any]:
        index_name = self._payments_index_name(partition)
        return self._http_json("GET", f"/{index_name}/_mapping")

    def get_index_settings(self, partition: str) -> dict[str, Any]:
        index_name = self._payments_index_name(partition)
        return self._http_json("GET", f"/{index_name}/_settings")

    def refresh_index(self, partition: str) -> dict[str, Any]:
        index_name = self._payments_index_name(partition)
        return self._http_json("POST", f"/{index_name}/_refresh")
