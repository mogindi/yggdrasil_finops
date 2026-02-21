#!/usr/bin/env python3
"""
Configure default CloudKitty hashmap costs.

Usage:
  source admin-openrc.sh
  python scripts/configure_cloudkitty_defaults.py
"""

import json
import argparse

from cloudkitty_client import CloudKittyClient, CloudKittyError, OpenStackAuthError


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Configure default CloudKitty hashmap costs")
    parser.add_argument("--debug", action="store_true", help="Enable detailed debug output for each step and API call")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    client = CloudKittyClient(debug=args.debug)
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
