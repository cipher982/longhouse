"""Every `/api/*` route lives on `api_app`, not on `app`.

`AGENTS.md` has said this for a long time and nothing enforced it. The rule
exists because the two apps are not interchangeable: `api_app` is what
`build_api_openapi_schema` reads, so a route registered directly on `app` serves
traffic while being absent from the OpenAPI schema, the generated TypeScript
types, and the iOS DTOs. It works in a browser and does not exist to any
generated client.

A grep cannot check this. Routers are registered in several places and a prefix
can be assembled at runtime, so "what does this app actually serve?" is only
answerable by asking the app.

The route table is read in a subprocess for two reasons. `zerg.main` resolves
settings at import time, so shaping the environment in this module would decide
those values for every test that imports it later -- the first draft of this file
did exactly that and broke five unrelated tests, including one whose whole point
is importing the app *without* `TESTING=1`. And an in-process app object can have
been mutated by whatever ran before; a child process reads a freshly constructed
one.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest
from cryptography.fernet import Fernet

API_MOUNT_PATH = "/api"

_DUMP_ROUTES = dedent(
    """
    import json
    from starlette.routing import Mount
    from zerg.main import api_app, app

    mounts = [
        {"path": r.path, "is_api_app": r.app is api_app}
        for r in app.routes
        if isinstance(r, Mount)
    ]
    outer = [
        {"path": r.path, "kind": type(r).__name__, "is_api_app_mount": isinstance(r, Mount) and r.app is api_app}
        for r in app.routes
        if isinstance(getattr(r, "path", None), str)
    ]
    inner = [r.path for r in api_app.routes if isinstance(getattr(r, "path", None), str)]
    print("ROUTES_JSON:" + json.dumps({"mounts": mounts, "outer": outer, "inner": inner}))
    """
)


@pytest.fixture(scope="module")
def routes() -> dict:
    server_dir = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment.update(
        {
            "DATABASE_URL": "sqlite://",
            "TESTING": "1",
            # Generated rather than pinned: settings validate this as a real
            # Fernet key, and a committed one that looks like a secret is worse
            # than a generated one that cannot be.
            "FERNET_SECRET": Fernet.generate_key().decode(),
            "JWT_SECRET": "api-surface-boundary-test",
            "INTERNAL_API_SECRET": "api-surface-boundary-test",
            "GOOGLE_CLIENT_ID": "api-surface-boundary-test",
            "GOOGLE_CLIENT_SECRET": "api-surface-boundary-test",
        }
    )
    completed = subprocess.run(
        [sys.executable, "-c", _DUMP_ROUTES],
        cwd=server_dir,
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    marker = "ROUTES_JSON:"
    line = next(
        (ln for ln in completed.stdout.splitlines() if ln.startswith(marker)),
        None,
    )
    assert line, f"route dump failed\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    return json.loads(line[len(marker) :])


def _under_api(path: str) -> bool:
    """`/apiary` is not under `/api`. A prefix test is not a path-boundary test."""
    return path == API_MOUNT_PATH or path.startswith(f"{API_MOUNT_PATH}/")


def test_api_is_served_by_exactly_one_mount_and_it_is_api_app(routes):
    api_mounts = [m for m in routes["mounts"] if m["path"] == API_MOUNT_PATH]
    assert len(api_mounts) == 1, (
        f"expected exactly one mount at {API_MOUNT_PATH}, found {len(api_mounts)}. Two mounts on one path means the second is unreachable."
    )
    assert api_mounts[0]["is_api_app"], (
        f"{API_MOUNT_PATH} is mounted, but not to api_app. The OpenAPI schema is "
        "built from api_app, so whatever is mounted here is what clients get typed."
    )


def test_no_route_on_the_outer_app_serves_an_api_path(routes):
    """Catches `app.include_router(r)` where r carries an /api prefix.

    Such a route serves traffic and is invisible to every generated client.
    """
    offenders = [f"{r['kind']} {r['path']}" for r in routes["outer"] if _under_api(r["path"]) and not r["is_api_app_mount"]]
    assert not offenders, (
        "these serve /api from the outer app instead of api_app, so they are "
        "absent from the OpenAPI schema and every generated client:\n  " + "\n  ".join(sorted(offenders))
    )


def test_api_app_routes_are_not_double_prefixed(routes):
    """api_app is mounted at /api, so a route declared `/api/foo` serves at /api/api/foo.

    Reachable, typed, and wrong -- the hardest kind of wrong to notice.
    """
    doubled = [p for p in routes["inner"] if p.startswith(f"{API_MOUNT_PATH}/")]
    assert not doubled, (
        "these carry an /api prefix on an app already mounted at "
        f"{API_MOUNT_PATH}, so they serve at /api/api/...:\n  " + "\n  ".join(sorted(doubled))
    )
