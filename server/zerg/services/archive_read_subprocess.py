"""One-request ASGI child for crash-isolated archive reads."""

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
    path = Path(parsed.database).expanduser()
    if not path.is_absolute():
        path = path.resolve()
    return f"sqlite:///file:{quote(str(path), safe='/')}?mode=ro&uri=true"


async def _main() -> None:
    payload = json.loads(sys.stdin.buffer.read())
    # These must be authoritative before importing settings or route modules.
    os.environ["AUTH_DISABLED"] = "1"
    os.environ["LONGHOUSE_ARCHIVE_READER_CHILD"] = "1"
    os.environ["LONGHOUSE_LIVE_CATALOG_ENABLED"] = "0"
    os.environ["LONGHOUSE_ARCHIVE_WORKER_ENABLED"] = "0"
    original_database_url = os.environ.get("DATABASE_URL", "")
    if not os.environ.get("LONGHOUSE_LIVE_DATABASE_URL") and not os.environ.get("LONGHOUSE_LIVE_DB_PATH"):
        from sqlalchemy.engine import make_url

        parsed = make_url(original_database_url)
        if parsed.drivername.startswith("sqlite") and parsed.database and parsed.database != ":memory:":
            archive_path = Path(parsed.database).expanduser()
            live_path = archive_path.with_name(f"{archive_path.stem}-live.db")
            if live_path.exists():
                os.environ["LONGHOUSE_LIVE_DATABASE_URL"] = _readonly_sqlite_url(f"sqlite:///{live_path}")
    explicit_live_path = os.environ.get("LONGHOUSE_LIVE_DB_PATH", "")
    if explicit_live_path:
        os.environ["LONGHOUSE_LIVE_DATABASE_URL"] = _readonly_sqlite_url(f"sqlite:///{explicit_live_path}")
    os.environ["DATABASE_URL"] = _readonly_sqlite_url(original_database_url)
    explicit_live_url = os.environ.get("LONGHOUSE_LIVE_DATABASE_URL", "")
    if explicit_live_url:
        os.environ["LONGHOUSE_LIVE_DATABASE_URL"] = _readonly_sqlite_url(explicit_live_url)

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
