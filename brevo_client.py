import base64
import json
import logging
import os
from urllib import error, request


class BrevoError(Exception):
    pass


class BrevoClient:
    def __init__(self, debug: bool = False):
        self.api_key = os.environ["BREVO_API_KEY"]
        self.endpoint = os.environ["BREVO_API_URL"]
        self.sender_email = os.environ["BREVO_SENDER_EMAIL"]
        self.sender_name = os.environ["BREVO_SENDER_NAME"]
        self.debug = debug
        self._logger = logging.getLogger(self.__class__.__name__)

    def _debug(self, message: str) -> None:
        if self.debug:
            self._logger.debug(message)

    def send_pdf(self, *, to_email: str, subject: str, html_content: str, filename: str, content: bytes) -> dict:
        if not self.api_key:
            raise BrevoError("BREVO_API_KEY is required to send email")
        if not to_email:
            raise BrevoError("Recipient email is required")

        payload = {
            "sender": {"email": self.sender_email, "name": self.sender_name},
            "to": [{"email": to_email}],
            "subject": subject,
            "htmlContent": html_content,
            "attachment": [{"name": filename, "content": base64.b64encode(content).decode("ascii")}],
        }
        body = json.dumps(payload).encode("utf-8")
        self._debug(f"Brevo API call: method=POST url={self.endpoint} to={to_email} subject={subject}")
        req = request.Request(
            self.endpoint,
            data=body,
            method="POST",
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "api-key": self.api_key,
            },
        )
        try:
            with request.urlopen(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8")
                self._debug(f"Brevo API response: status={resp.status} body={raw[:500]}")
                return json.loads(raw) if raw else {"status": resp.status}
        except error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="ignore")
            self._debug(f"Brevo API error: status={exc.code} body={details[:500]}")
            raise BrevoError(f"Brevo API failed with status {exc.code}: {details}") from exc
        except error.URLError as exc:
            self._debug(f"Brevo request error: reason={exc.reason}")
            raise BrevoError(f"Brevo request failed: {exc.reason}") from exc
