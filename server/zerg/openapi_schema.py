from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi


def build_api_openapi_schema(api_app: FastAPI) -> dict[str, Any]:
    """Build the public OpenAPI schema for the mounted /api sub-app."""
    openapi_schema = get_openapi(
        title="Longhouse API",
        version="1.0.0",
        description=(
            "Complete REST API specification for Longhouse. "
            "This schema is the single source of truth for frontend-backend contracts."
        ),
        routes=api_app.routes,
    )

    openapi_schema["paths"] = {
        f"/api{path}": ops for path, ops in openapi_schema.get("paths", {}).items()
    }
    openapi_schema["servers"] = [
        {"url": "http://localhost:8001", "description": "Development server"},
        {"url": "https://api.longhouse.ai", "description": "Production server"},
    ]
    return openapi_schema


def get_openapi_output_path() -> Path:
    return Path(__file__).resolve().parents[2] / "openapi.json"


def export_openapi_schema(schema: dict[str, Any], path: Path | None = None) -> Path:
    output_path = path or get_openapi_output_path()
    with output_path.open("w") as fh:
        json.dump(schema, fh, indent=2)
        fh.write("\n")
    return output_path
