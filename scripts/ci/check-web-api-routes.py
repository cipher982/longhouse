#!/usr/bin/env python3
"""Every API path the web client builds exists in the served route table.

The web client's API modules (web/src/shared/api/*.ts) hand-build request
paths. A path no router serves fails only at runtime, as a 404 the caller may
swallow: the pause-request answer posted to /api/timeline/sessions/... for
months, and the workflow-runs panel kept calling routes deleted on 2026-09-01.

This reads each path-shaped string literal in those modules, resolves the
module's path constants (TIMELINE_SESSIONS_PREFIX and friends), drops query
strings, and requires a route on the served api_app (mounted at /api) with the
same segments, where a `${...}` segment matches any path parameter. Paths only,
not methods. Routes hidden from OpenAPI (include_in_schema=False) count.

Run from server/: uv run --no-sync python ../scripts/ci/check-web-api-routes.py
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
API_DIR = REPO_ROOT / "web" / "src" / "shared" / "api"
# base.ts builds URLs from any path its callers pass; it names no endpoint.
SKIP_FILES = {"base.ts"}

_CONST_RE = re.compile(r"const ([A-Z_]+)\s*=\s*([\"`])(.*?)\2;")
_LITERAL_RE = re.compile(r"([\"`])((?:/|\$\{[A-Z_]+\})[^\"`]*?)\1")
_CONST_REF_RE = re.compile(r"\$\{([A-Z_]+)\}")


def _resolve(text: str, consts: dict[str, str]) -> str:
    return _CONST_REF_RE.sub(lambda m: consts.get(m.group(1), m.group(0)), text)


def web_paths(source: str) -> list[str]:
    """Path-shaped literals in one module, constants resolved, as /api/... segment paths."""
    consts = {m.group(1): m.group(3) for m in _CONST_RE.finditer(source)}
    for _ in range(len(consts)):
        consts = {k: _resolve(v, consts) for k, v in consts.items()}
    paths: list[str] = []
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(("//", "*", "/*")) or re.match(r"(export )?const [A-Z_]+\s*=", stripped):
            continue
        for match in _LITERAL_RE.finditer(line):
            literal = _resolve(match.group(2), consts)
            if not literal.startswith("/"):
                continue
            segments = []
            for segment in literal.split("?", 1)[0].strip("/").split("/"):
                if re.fullmatch(r"\$\{[^}]*\}", segment):
                    segments.append("{}")
                else:
                    # A trailing `${...}` glued to a segment is a query-string builder.
                    segment = re.sub(r"\$\{.*$", "", segment)
                    if segment:
                        segments.append(segment)
            paths.append("/api/" + "/".join(segments))
    return paths


def _segments(path: str) -> list[str]:
    return [s for s in path.strip("/").split("/") if s]


def route_exists(web_path: str, routes: list[str]) -> bool:
    want = _segments(web_path)
    for route in routes:
        have = _segments(route)
        if len(have) != len(want):
            continue
        if all(w == h or w == "{}" or (h.startswith("{") and h.endswith("}")) for w, h in zip(want, have)):
            return True
    return False


def missing_paths(sources: dict[str, str], routes: list[str]) -> list[tuple[str, str]]:
    missing = []
    for name, source in sorted(sources.items()):
        for path in sorted(set(web_paths(source))):
            if not route_exists(path, routes):
                missing.append((name, path))
    return missing


def served_routes() -> list[str]:
    os.environ.setdefault("TESTING", "1")
    os.environ.setdefault("AUTH_DISABLED", "1")
    os.environ.setdefault("DATABASE_URL", "sqlite://")
    os.environ.setdefault("JWT_SECRET", secrets.token_urlsafe(24))
    os.environ.setdefault("INTERNAL_API_SECRET", secrets.token_urlsafe(24))
    from cryptography.fernet import Fernet

    os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
    sys.path.insert(0, str(REPO_ROOT / "server"))
    logging.disable(logging.INFO)  # app import logs its CORS/config choices
    from fastapi import routing

    from zerg.main import api_app

    # include_router() adds lazy router nodes; iter_route_contexts flattens them
    # the way get_openapi does, hidden routes included.
    return ["/api" + context.path for context in routing.iter_route_contexts(api_app.routes)]


def main() -> int:
    sources = {f.name: f.read_text() for f in sorted(API_DIR.glob("*.ts")) if f.name not in SKIP_FILES}
    routes = served_routes()
    missing = missing_paths(sources, routes)
    checked = sum(len(set(web_paths(s))) for s in sources.values())
    if missing:
        print("Web API paths with no served route:")
        for name, path in missing:
            print(f"  web/src/shared/api/{name}: {path}")
        return 1
    print(f"web api routes: {checked} paths in {len(sources)} modules all served")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
