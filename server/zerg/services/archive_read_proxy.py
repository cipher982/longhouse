"""Crash-isolated reads for cold archive-backed HTTP surfaces.

The Runtime Host authenticates against the live catalog, then delegates only
bounded GET requests to a fresh helper process.  A corrupt/native-failing cold
SQLite read can therefore fail one request without terminating the hot API.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import sys

from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from starlette.responses import Response

logger = logging.getLogger(__name__)

_TIMELINE_SESSION_READ = re.compile(
    r"^/timeline/sessions/[^/]+(?:$|/(?:thread|turns(?:/\d+)?|events|projection|workspace|mobile-tail|preview|graph|workflows)$)"
)
_AGENTS_SESSION_READ = re.compile(
    r"^/agents/sessions/[^/]+(?:$|/(?:thread|tail|turns(?:/\d+)?|events|projection|workspace|preview|graph|workflows)$)"
)
_EXACT_ARCHIVE_READS = {
    "/timeline/recall",
    "/timeline/sessions/semantic",
    "/timeline/filters",
    "/timeline/sessions/summary",
    "/agents/recall",
    "/agents/sessions/semantic",
    "/agents/sessions/summary",
    "/agents/sessions/wall",
}
_PASSTHROUGH_HEADERS = {
    "cache-control",
    "content-disposition",
    "content-language",
    "content-type",
    "etag",
    "last-modified",
    "x-limit-cap",
}
_ARCHIVE_READ_SLOTS = asyncio.Semaphore(max(1, int(os.getenv("LONGHOUSE_ARCHIVE_READ_MAX_PROCESSES", "4"))))


def normalized_api_path(request: Request) -> str:
    path = request.url.path
    return path[4:] if path.startswith("/api/") else path


def should_proxy_archive_read(request: Request) -> bool:
    if request.method.upper() not in {"GET", "HEAD"}:
        return False
    path = normalized_api_path(request)
    if path in _EXACT_ARCHIVE_READS:
        return True
    if path == "/timeline/sessions":
        return "query" in request.query_params or request.query_params.get("mode", "lexical") != "lexical"
    if path == "/agents/sessions":
        return "query" in request.query_params or request.query_params.get("mode", "lexical") != "lexical"
    return bool(_TIMELINE_SESSION_READ.fullmatch(path) or _AGENTS_SESSION_READ.fullmatch(path))


async def proxy_archive_read(request: Request) -> Response:
    payload = {
        "method": request.method.upper(),
        "path": normalized_api_path(request),
        "query": request.url.query,
    }
    env = dict(os.environ)
    env.update(
        {
            "AUTH_DISABLED": "1",
            "LONGHOUSE_LIVE_CATALOG_ENABLED": "0",
            "LONGHOUSE_ARCHIVE_WORKER_ENABLED": "0",
            "LONGHOUSE_ARCHIVE_READER_CHILD": "1",
        }
    )
    try:
        await asyncio.wait_for(_ARCHIVE_READ_SLOTS.acquire(), timeout=1.0)
    except TimeoutError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "archive_read_pressure", "message": "Archive readers are busy; retry shortly."},
        ) from None
    timeout = max(1.0, float(os.getenv("LONGHOUSE_ARCHIVE_READ_TIMEOUT_SECONDS", "15")))
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "zerg.services.archive_read_subprocess",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(json.dumps(payload).encode("utf-8")),
                timeout=timeout,
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "archive_read_timeout",
                    "message": "Archive read timed out; live control remains available.",
                },
            ) from None
    except OSError as exc:
        logger.warning("Could not start archive read child for %s: %s", payload["path"], exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "archive_read_unavailable", "message": "Archive reader could not start."},
        ) from None
    finally:
        _ARCHIVE_READ_SLOTS.release()
    if proc.returncode != 0:
        logger.warning(
            "Archive read child failed returncode=%s path=%s stderr=%s",
            proc.returncode,
            payload["path"],
            stderr.decode("utf-8", errors="replace")[-1000:],
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "archive_read_unavailable",
                "message": "Archive detail is temporarily unavailable; live control remains available.",
            },
        )
    try:
        result = json.loads(stdout)
        body = base64.b64decode(result["body_b64"])
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        logger.warning("Archive read child returned an invalid envelope for %s", payload["path"])
        raise HTTPException(status_code=503, detail={"code": "archive_read_invalid_response"}) from None
    headers = {str(k): str(v) for k, v in result.get("headers", {}).items() if str(k).lower() in _PASSTHROUGH_HEADERS}
    return Response(content=body, status_code=int(result["status_code"]), headers=headers)
