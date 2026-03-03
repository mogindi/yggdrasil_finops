from unittest.mock import patch

import pytest

from cloudkitty_client import CloudKittyApiError, CloudKittyClient, CloudKittyError
from currency import get_default_currency


def test_default_currency_is_dkk(monkeypatch):
    monkeypatch.delenv("CLOUDKITTY_CURRENCY", raising=False)
    assert get_default_currency() == "DKK"


def test_currency_is_uppercased(monkeypatch):
    monkeypatch.setenv("CLOUDKITTY_CURRENCY", "eur")
    assert get_default_currency() == "EUR"


def test_cloudkitty_currency_validation_fails_on_mismatch(monkeypatch):
    monkeypatch.setenv("OS_AUTH_URL", "https://keystone.example/v3")
    monkeypatch.setenv("OS_USERNAME", "u")
    monkeypatch.setenv("OS_PASSWORD", "p")
    monkeypatch.setenv("OS_PROJECT_ID", "proj")
    monkeypatch.setenv("CLOUDKITTY_ENDPOINT", "https://ck.example")

    client = CloudKittyClient()
    with patch.object(client, "get_cloudkitty_currency", return_value="EUR"):
        with pytest.raises(CloudKittyError):
            client.validate_currency("DKK")


def test_cloudkitty_currency_validation_falls_back_from_405_info_endpoint(monkeypatch):
    monkeypatch.setenv("OS_AUTH_URL", "https://keystone.example/v3")
    monkeypatch.setenv("OS_USERNAME", "u")
    monkeypatch.setenv("OS_PASSWORD", "p")
    monkeypatch.setenv("OS_PROJECT_ID", "proj")
    monkeypatch.setenv("CLOUDKITTY_ENDPOINT", "https://ck.example")

    client = CloudKittyClient()

    def fake_request(method, path, params=None, body=None):
        if path == "/v1/info" and method == "GET":
            raise CloudKittyApiError("method not allowed", status_code=405, url=f"https://ck.example{path}")
        if path == "/v1/info" and method == "POST":
            return {"info": {"currency": "dkk"}}
        return {}

    with patch.object(client, "request", side_effect=fake_request):
        client.validate_currency("DKK")


def test_cloudkitty_currency_validation_raises_cloudkittyerror_when_info_endpoints_reject_methods(monkeypatch):
    monkeypatch.setenv("OS_AUTH_URL", "https://keystone.example/v3")
    monkeypatch.setenv("OS_USERNAME", "u")
    monkeypatch.setenv("OS_PASSWORD", "p")
    monkeypatch.setenv("OS_PROJECT_ID", "proj")
    monkeypatch.setenv("CLOUDKITTY_ENDPOINT", "https://ck.example")

    client = CloudKittyClient()

    def fake_request(method, path, params=None, body=None):
        raise CloudKittyApiError("method not allowed", status_code=405, url=f"https://ck.example{path}")

    with patch.object(client, "request", side_effect=fake_request):
        with pytest.raises(CloudKittyError, match="accepted GET or POST"):
            client.validate_currency("DKK")
