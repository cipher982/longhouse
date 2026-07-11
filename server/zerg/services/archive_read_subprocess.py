"""One-request ASGI child for crash-isolated archive operations."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
from pathlib import Path
from urllib.parse import quote


def _readonly_sqlite_url(raw: str) -> str:
    """Return a SQLite file URL that refuses writes in the helper process."""

    from sqlalchemy.engine import make_url

    if not raw.startswith("sqlite"):
        return raw
    parsed = make_url(raw)
    if not parsed.database or parsed.database == ":memory:":
        return raw
    if parsed.database.startswith("file:") and parsed.query.get("mode") == "ro" and parsed.query.get("uri") == "true":
        return raw
    path = Path(parsed.database).expanduser()
    if not path.is_absolute():
        path = path.resolve()
    return f"sqlite:///file:{quote(str(path), safe='/')}?mode=ro&uri=true"


def _request_is_read_only(method: str, path: str) -> bool:
    return method in {"GET", "HEAD"} or (method == "POST" and path == "/agents/source-lines/claims")


async def _main() -> None:
    payload = json.loads(sys.stdin.buffer.read())
    method = str(payload.get("method") or "GET").upper()
    path = str(payload["path"])
    read_only = _request_is_read_only(method, path)
    original_database_url = os.environ.get("DATABASE_URL", "")
    if read_only:
        os.environ["DATABASE_URL"] = _readonly_sqlite_url(original_database_url)

    import httpx

    from zerg.database import use_archive_database_for_process

    use_archive_database_for_process()
    from zerg.main import api_app

    query = str(payload.get("query") or "")
    url = f"{path}?{query}" if query else path
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app),
        base_url="http://archive-reader",
    ) as client:
        response = await client.request(
            method,
            url,
            headers={str(key): str(value) for key, value in (payload.get("headers") or {}).items()},
            content=base64.b64decode(payload.get("body_b64") or ""),
        )
    result = {
        "status_code": response.status_code,
        "headers": dict(response.headers),
        "body_b64": base64.b64encode(response.content).decode("ascii"),
    }
    sys.stdout.write(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    asyncio.run(_main())
