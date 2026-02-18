#!/usr/bin/env python3
"""
Configure default CloudKitty hashmap costs.

Usage:
  source admin-openrc.sh
  python scripts/configure_cloudkitty_defaults.py
"""

import json

from cloudkitty_client import CloudKittyClient, CloudKittyError, OpenStackAuthError


def main() -> int:
    client = CloudKittyClient()
    try:
        client.authenticate()
        summary = client.ensure_default_hashmap_pricing()
    except (OpenStackAuthError, CloudKittyError) as exc:
        print(f"ERROR: {exc}")
        return 1

    print("CloudKitty default hashmap pricing ensured.")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
