import os


DEFAULT_CURRENCY = "DKK"


def get_default_currency() -> str:
    return os.environ.get("CLOUDKITTY_CURRENCY", DEFAULT_CURRENCY).upper()
