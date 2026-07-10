"""One-request ASGI child for crash-isolated archive reads."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys


async def _main() -> None:
    payload = json.loads(sys.stdin.buffer.read())
    # These must be authoritative before importing settings or route modules.
    os.environ["AUTH_DISABLED"] = "1"
    os.environ["LONGHOUSE_LIVE_CATALOG_ENABLED"] = "0"
    os.environ["LONGHOUSE_ARCHIVE_WORKER_ENABLED"] = "0"

    import httpx

    from zerg.main import api_app

    path = str(payload["path"])
    query = str(payload.get("query") or "")
    url = f"{path}?{query}" if query else path
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app),
        base_url="http://archive-reader",
    ) as client:
        response = await client.request(str(payload.get("method") or "GET"), url)
    result = {
        "status_code": response.status_code,
        "headers": dict(response.headers),
        "body_b64": base64.b64encode(response.content).decode("ascii"),
    }
    sys.stdout.write(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    asyncio.run(_main())
