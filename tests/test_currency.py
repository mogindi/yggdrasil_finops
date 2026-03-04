import pytest

from currency import get_default_currency


def test_default_currency_is_dkk(monkeypatch):
    monkeypatch.delenv("CLOUDKITTY_CURRENCY", raising=False)
    monkeypatch.setenv("CLOUDKITTY_CURRENCY", "DKK")
    assert get_default_currency() == "DKK"


def test_currency_is_uppercased(monkeypatch):
    monkeypatch.setenv("CLOUDKITTY_CURRENCY", "eur")
    assert get_default_currency() == "EUR"
