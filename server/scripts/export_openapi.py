#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys

from cryptography.fernet import Fernet


def _bootstrap_contract_env() -> None:
    os.environ.setdefault("TESTING", "1")
    os.environ.setdefault("AUTH_DISABLED", "1")
    os.environ.setdefault("DATABASE_URL", "sqlite://")
    os.environ.setdefault("JWT_SECRET", secrets.token_urlsafe(24))
    os.environ.setdefault("INTERNAL_API_SECRET", secrets.token_urlsafe(24))
    os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())


def main() -> int:
    parser = argparse.ArgumentParser(description="Export Longhouse OpenAPI schema")
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print schema JSON to stdout after writing apps/zerg/openapi.json",
    )
    args = parser.parse_args()

    _bootstrap_contract_env()

    from zerg.main import api_app
    from zerg.openapi_schema import build_api_openapi_schema
    from zerg.openapi_schema import export_openapi_schema

    schema = build_api_openapi_schema(api_app)
    export_openapi_schema(schema)

    if args.stdout:
        json.dump(schema, sys.stdout)
        sys.stdout.write("\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
