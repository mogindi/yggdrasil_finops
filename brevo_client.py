import base64
import json
import os
from urllib import error, request


class BrevoError(Exception):
    pass


class BrevoClient:
    def __init__(self):
        self.api_key = os.environ.get("BREVO_API_KEY", "")
        self.endpoint = os.environ.get("BREVO_API_URL", "https://api.brevo.com/v3/smtp/email")
        self.sender_email = os.environ.get("BREVO_SENDER_EMAIL", "noreply@example.com")
        self.sender_name = os.environ.get("BREVO_SENDER_NAME", "Yggdrasil FinOps")

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
                return json.loads(raw) if raw else {"status": resp.status}
        except error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="ignore")
            raise BrevoError(f"Brevo API failed with status {exc.code}: {details}") from exc
        except error.URLError as exc:
            raise BrevoError(f"Brevo request failed: {exc.reason}") from exc
