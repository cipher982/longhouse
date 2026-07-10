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
from collections.abc import Iterable
from typing import Any

from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from starlette.responses import Response

logger = logging.getLogger(__name__)

_TIMELINE_SESSION_READ = re.compile(
    r"^/timeline/sessions/[^/]+(?:$|/(?:thread|turns(?:/\d+)?|events|projection|workspace|mobile-tail|preview|graph|workflows)$)"
)
_EXACT_ARCHIVE_READS = {
    "/timeline/recall",
    "/timeline/sessions/semantic",
    "/timeline/filters",
    "/timeline/sessions/summary",
}
_UNBOUNDED_AGENT_READ = re.compile(r"^/agents/sessions/[^/]+/(?:export|archive-bundle)$")
_PASSTHROUGH_HEADERS = {
    "cache-control",
    "content-disposition",
    "content-language",
    "content-type",
    "etag",
    "last-modified",
    "x-limit-cap",
}
_ARCHIVE_READ_SLOTS = asyncio.Semaphore(4)


def normalized_api_path(request: Request) -> str:
    path = request.url.path
    return path[4:] if path.startswith("/api/") else path


def _depends_on_archive_db(dependant: Any) -> bool:
    from zerg.database import get_db

    for dependency in getattr(dependant, "dependencies", ()):
        if dependency.call is get_db or _depends_on_archive_db(dependency):
            return True
    return False


def _agent_route_reads_archive(path: str, method: str, routes: Iterable[Any]) -> bool:
    if _UNBOUNDED_AGENT_READ.fullmatch(path):
        return False
    for route in routes:
        methods = getattr(route, "methods", ()) or ()
        path_regex = getattr(route, "path_regex", None)
        if method not in methods or path_regex is None or path_regex.fullmatch(path) is None:
            continue
        dependant = getattr(route, "dependant", None)
        return dependant is not None and _depends_on_archive_db(dependant)
    return False


def should_proxy_archive_read(request: Request, *, routes: Iterable[Any] = ()) -> bool:
    if request.method.upper() not in {"GET", "HEAD"}:
        return False
    path = normalized_api_path(request)
    if path == "/timeline/sessions/stream":
        return False
    if path in _EXACT_ARCHIVE_READS:
        return True
    if path == "/timeline/sessions":
        return "query" in request.query_params or request.query_params.get("mode", "lexical") != "lexical"
    if path == "/agents/sessions":
        return "query" in request.query_params or request.query_params.get("mode", "lexical") != "lexical"
    if path.startswith("/agents/") and _agent_route_reads_archive(path, request.method.upper(), routes):
        return True
    return bool(_TIMELINE_SESSION_READ.fullmatch(path))


async def proxy_archive_read(request: Request) -> Response:
    payload = {
        "method": request.method.upper(),
        "path": normalized_api_path(request),
        "query": request.url.query,
    }
    env = dict(os.environ)
    env["AUTH_DISABLED"] = "1"
    try:
        await asyncio.wait_for(_ARCHIVE_READ_SLOTS.acquire(), timeout=1.0)
    except TimeoutError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "archive_read_pressure", "message": "Archive readers are busy; retry shortly."},
        ) from None
    timeout = 30.0
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
