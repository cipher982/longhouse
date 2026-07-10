"""Crash-isolated requests for cold archive-backed HTTP surfaces.

The Runtime Host delegates any route that depends on the cold database to a
fresh helper process. A corrupt/native-failing SQLite operation can therefore
fail one request without terminating the hot API.
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


def _route_uses_archive_db(path: str, method: str, routes: Iterable[Any]) -> bool:
    for route in routes:
        methods = getattr(route, "methods", ()) or ()
        path_regex = getattr(route, "path_regex", None)
        if method not in methods or path_regex is None or path_regex.fullmatch(path) is None:
            continue
        dependant = getattr(route, "dependant", None)
        return dependant is not None and _depends_on_archive_db(dependant)
    return False


def should_proxy_archive_request(request: Request, *, routes: Iterable[Any] = ()) -> bool:
    path = normalized_api_path(request)
    method = request.method.upper()
    if path == "/timeline/sessions/stream":
        return False
    if method in {"GET", "HEAD"} and path in _EXACT_ARCHIVE_READS:
        return True
    if method in {"GET", "HEAD"} and path == "/timeline/sessions":
        return "query" in request.query_params or request.query_params.get("mode", "lexical") != "lexical"
    if method in {"GET", "HEAD"} and path == "/agents/sessions":
        return "query" in request.query_params or request.query_params.get("mode", "lexical") != "lexical"
    if method in {"GET", "HEAD"} and _TIMELINE_SESSION_READ.fullmatch(path):
        return True
    return _route_uses_archive_db(path, method, routes)


async def proxy_archive_request(request: Request) -> Response:
    body = await request.body()
    payload = {
        "method": request.method.upper(),
        "path": normalized_api_path(request),
        "query": request.url.query,
        "headers": dict(request.headers),
        "body_b64": base64.b64encode(body).decode("ascii"),
    }
    env = dict(os.environ)
    try:
        await asyncio.wait_for(_ARCHIVE_READ_SLOTS.acquire(), timeout=1.0)
    except TimeoutError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "archive_request_pressure", "message": "Archive workers are busy; retry shortly."},
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
                    "code": "archive_request_timeout",
                    "message": "Archive operation timed out; live control remains available.",
                },
            ) from None
    except OSError as exc:
        logger.warning("Could not start archive request child for %s: %s", payload["path"], exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "archive_request_unavailable", "message": "Archive worker could not start."},
        ) from None
    finally:
        _ARCHIVE_READ_SLOTS.release()
    if proc.returncode != 0:
        logger.warning(
            "Archive request child failed returncode=%s path=%s stderr=%s",
            proc.returncode,
            payload["path"],
            stderr.decode("utf-8", errors="replace")[-1000:],
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "archive_request_unavailable",
                "message": "Archive operation is temporarily unavailable; live control remains available.",
            },
        )
    try:
        result = json.loads(stdout)
        body = base64.b64decode(result["body_b64"])
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        logger.warning("Archive request child returned an invalid envelope for %s", payload["path"])
        raise HTTPException(status_code=503, detail={"code": "archive_request_invalid_response"}) from None
    headers = {str(k): str(v) for k, v in result.get("headers", {}).items() if str(k).lower() in _PASSTHROUGH_HEADERS}
    return Response(content=body, status_code=int(result["status_code"]), headers=headers)
