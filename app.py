#!/usr/bin/env python3
"""Compatibility entrypoint.

The monolithic HTTP server has been removed in favor of microservice-only runtime.
Use `gateway_service.py` (plus dedicated backend services) for all API traffic.
"""

from gateway_service import GatewayHandler, run as run_gateway

# Backwards-compatible alias used by older imports/tests.
CostHandler = GatewayHandler


def run() -> None:
    run_gateway()


if __name__ == "__main__":
    run()
