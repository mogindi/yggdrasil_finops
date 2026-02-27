#!/usr/bin/env python3
"""
Configure default CloudKitty hashmap costs.

Usage:
  source admin-openrc.sh
  python scripts/configure_cloudkitty_defaults.py
"""

import json
import argparse
from pathlib import Path
from typing import Any

from cloudkitty_client import CloudKittyClient, CloudKittyError, OpenStackAuthError


DEFAULT_PRICING: dict[str, list[dict[str, Any]]] = {
    "instance": [
        {"value": "small", "cost": 0.025},
        {"value": "medium", "cost": 0.06},
        {"value": "large", "cost": 0.10},
    ],
    "volume": [
        {"value": "standard", "cost": 0.08},
        {"value": "ssd", "cost": 0.15},
    ],
    # Intentionally empty by default: networking egress should not be charged here.
    "network.bw.out": [],
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Configure default CloudKitty hashmap costs")
    parser.add_argument("--debug", action="store_true", help="Enable detailed debug output for each step and API call")
    parser.add_argument(
        "--pricing-config",
        type=Path,
        help="Path to a JSON file with CloudKitty hashmap pricing configuration",
    )
    return parser.parse_args()


def _validate_pricing_config(pricing: Any) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(pricing, dict):
        raise ValueError("Pricing configuration must be a JSON object")

    validated: dict[str, list[dict[str, Any]]] = {}
    for service_name, mappings in pricing.items():
        if not isinstance(service_name, str):
            raise ValueError("Each service name must be a string")
        if not isinstance(mappings, list):
            raise ValueError(f"Mappings for service '{service_name}' must be a list")

        validated_mappings: list[dict[str, Any]] = []
        for mapping in mappings:
            if not isinstance(mapping, dict):
                raise ValueError(f"Each mapping for service '{service_name}' must be an object")
            if "value" not in mapping or "cost" not in mapping:
                raise ValueError(f"Each mapping for service '{service_name}' must include 'value' and 'cost'")
            validated_mappings.append({"value": str(mapping["value"]), "cost": float(mapping["cost"])})
        validated[service_name] = validated_mappings
    return validated


def load_pricing_config(path: Path | None) -> dict[str, list[dict[str, Any]]]:
    if path is None:
        return DEFAULT_PRICING

    payload = json.loads(path.read_text(encoding="utf-8"))
    return _validate_pricing_config(payload)


def main() -> int:
    args = parse_args()
    try:
        pricing = load_pricing_config(args.pricing_config)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"ERROR: invalid pricing configuration: {exc}")
        return 1

    client = CloudKittyClient(debug=args.debug)
    try:
        client.authenticate()
        summary = client.ensure_default_hashmap_pricing(pricing)
    except (OpenStackAuthError, CloudKittyError) as exc:
        print(f"ERROR: {exc}")
        return 1

    print("CloudKitty default hashmap pricing ensured.")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
