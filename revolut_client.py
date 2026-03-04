import json
import logging
import os
import ssl
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from urllib import request
from urllib.error import HTTPError, URLError


class RevolutError(RuntimeError):
    pass


class RevolutApiError(RevolutError):
    def __init__(self, message: str, status_code: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class RevolutBusinessClient:
    def __init__(self, debug: bool = False) -> None:
        self.endpoint = os.environ["REVOLUT_BUSINESS_API_URL"].rstrip("/")
        self.orders_path = os.environ["REVOLUT_ORDERS_PATH"]
        self.api_key = os.environ.get("REVOLUT_API_KEY", "").strip()
        self.verify = os.environ["OS_VERIFY"].lower() not in {"0", "false", "no"}
        self._ssl_ctx = ssl.create_default_context() if self.verify else ssl._create_unverified_context()
        self.debug = debug
        self._logger = logging.getLogger(self.__class__.__name__)

    def _debug(self, message: str) -> None:
        if self.debug:
            self._logger.debug(message)

    @staticmethod
    def _to_minor_units(amount: float) -> int:
        decimal_amount = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return int(decimal_amount * 100)

    def _http_json(self, method: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
        if not self.api_key:
            raise RevolutError("REVOLUT_API_KEY is required")

        url = f"{self.endpoint}{path}"
        self._debug(f"Revolut API call: method={method} url={url} body_keys={sorted(body.keys())}")
        data = json.dumps(body).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        req = request.Request(url, method=method, data=data, headers=headers)
        try:
            with request.urlopen(req, context=self._ssl_ctx, timeout=20) as resp:
                payload = resp.read().decode("utf-8")
                self._debug(f"Revolut API response: method={method} url={url} status={resp.status} body={payload[:500]}")
                return json.loads(payload) if payload else {}
        except HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            self._debug(f"Revolut API error: method={method} url={url} status={exc.code} body={error_body[:500]}")
            raise RevolutApiError(f"Revolut request failed ({exc.code})", status_code=exc.code, body=error_body) from exc
        except URLError as exc:
            self._debug(f"Revolut connection error: method={method} url={url} reason={exc.reason}")
            raise RevolutError(f"Failed to connect to Revolut Business API at {self.endpoint}: {exc.reason}") from exc

    def create_order(self, *, order_id: str, amount: float, currency: str, description: str, customer_email: str, success_url: str | None, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "amount": self._to_minor_units(amount),
            "currency": currency,
            "merchant_order_ext_ref": order_id,
            "description": description,
            "customer": {"email": customer_email} if customer_email else {},
            "metadata": metadata or {},
        }
        if success_url:
            body["redirect_url"] = success_url
        return self._http_json("POST", self.orders_path, body)
