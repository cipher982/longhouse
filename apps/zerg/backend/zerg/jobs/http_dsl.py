"""HTTP DSL executor for simple declarative jobs.

Allows defining jobs as pure HTTP calls without Python code:
{
    "method": "POST",
    "url": "https://api.example.com/trigger",
    "headers": {"Authorization": "Bearer ${API_KEY}"},
    "timeout_seconds": 30,
    "success_codes": [200, 201, 204]
}

Environment variable expansion: ${VAR_NAME} in url, headers, body
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any
from typing import Awaitable
from typing import Callable

import httpx

logger = logging.getLogger(__name__)


class HTTPJobError(Exception):
    """HTTP DSL job failed."""

    pass


# Valid HTTP methods
VALID_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}


def validate_http_config(config: dict[str, Any]) -> None:
    """
    Validate HTTP DSL config at job registration time.

    Args:
        config: HTTP DSL configuration dict

    Raises:
        ValueError: If config is invalid
    """
    if "url" not in config:
        raise ValueError("HTTP DSL requires 'url'")

    method = config.get("method", "GET").upper()
    if method not in VALID_METHODS:
        raise ValueError(f"Invalid HTTP method: {method}")

    timeout = config.get("timeout_seconds", 30)
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError(f"Invalid timeout: {timeout}")

    success_codes = config.get("success_codes", [200])
    if not isinstance(success_codes, list) or not all(isinstance(c, int) for c in success_codes):
        raise ValueError("success_codes must be list of integers")


def _expand_env_vars(value: str) -> str:
    """
    Expand ${VAR_NAME} patterns. Missing vars become empty string.

    Logs warning for missing vars (but doesn't fail).
    """

    def replacer(match: re.Match) -> str:
        var_name = match.group(1)
        val = os.environ.get(var_name)
        if val is None:
            logger.warning("Environment variable not set: %s", var_name)
            return ""
        return val

    return re.sub(r"\$\{(\w+)\}", replacer, value)


def _expand_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Recursively expand env vars in dict values."""
    result = {}
    for k, v in data.items():
        if isinstance(v, str):
            result[k] = _expand_env_vars(v)
        elif isinstance(v, dict):
            result[k] = _expand_dict(v)
        else:
            result[k] = v
    return result


def create_http_executor(config: dict[str, Any]) -> Callable[[], Awaitable[dict[str, Any]]]:
    """
    Create async executor function from HTTP DSL config.

    Environment variable expansion: ${VAR_NAME} in url, headers, body

    Args:
        config: HTTP DSL configuration dict

    Returns:
        Async function that executes the HTTP call

    Raises:
        ValueError: If config is invalid
    """
    # Validate at creation time
    validate_http_config(config)

    async def run() -> dict[str, Any]:
        method = config.get("method", "GET").upper()
        url = _expand_env_vars(config["url"])
        headers = {k: _expand_env_vars(str(v)) for k, v in config.get("headers", {}).items()}
        body = config.get("body")
        timeout = config.get("timeout_seconds", 30)
        success_codes = config.get("success_codes", [200])

        # Expand body if it's a dict
        if isinstance(body, dict):
            body = _expand_dict(body)
        elif isinstance(body, str):
            body = _expand_env_vars(body)

        logger.info("HTTP DSL: %s %s", method, url)

        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(
                method=method,
                url=url,
                headers=headers,
                json=body if isinstance(body, dict) else None,
                content=body if isinstance(body, str) else None,
            )

        if response.status_code not in success_codes:
            raise HTTPJobError(f"HTTP {response.status_code} (expected {success_codes}): " f"{response.text[:500]}")

        logger.info("HTTP DSL completed: %d %s", response.status_code, url)

        # Return metadata (truncate large responses)
        return {
            "status_code": response.status_code,
            "response_size": len(response.content),
            "response_preview": response.text[:200] if response.text else None,
        }

    return run


__all__ = [
    "HTTPJobError",
    "create_http_executor",
    "validate_http_config",
]
