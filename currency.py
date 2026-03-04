import os


def get_default_currency() -> str:
    return os.environ["CLOUDKITTY_CURRENCY"].upper()
