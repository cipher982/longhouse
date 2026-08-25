"""The access log must never write a working share token to disk.

A share token is the entire authority to read a shared transcript, and it
travels in the URL path. The access log records the path of every request, so
an unredacted line hands anyone with log access a live credential — including
whoever ships those logs somewhere else.

Session sharing is currently disabled (see ``zerg/routers/session_shares.py``),
so no endpoint mints a token and these paths no longer resolve to a handler.
That does not retire this guard, it sharpens it: redaction is decided from the
path shape before routing, so it has to hold for a request that 404s or falls
through to the SPA catch-all exactly as it held for one that succeeded. A
token pasted at a dead URL is still a live credential in the log line, and a
revived share feature must not have to re-derive this.

This drives the real outer app (the middleware is installed there, not on
``api_app``) so the assertions cover the wire paths as the middleware sees them.
"""

from __future__ import annotations

import logging
import os

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "lh-access-log-tests-secret")
os.environ.setdefault("INTERNAL_API_SECRET", "lh-test-internal")
os.environ.setdefault("GOOGLE_CLIENT_ID", "lh-test-google-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "lh-test-google-client")

from zerg.main import app  # noqa: E402

ACCESS_LOGGER = "zerg.middleware.access_log"
# Shaped like a real one (``create_session_share`` mints ``lhshr_<id>.<sig>``),
# and deliberately not minted by anything: the middleware redacts on path
# shape, so it cannot depend on the token being resolvable.
# Shaped like a share token so the redactor matches the route, but
# deliberately low-entropy -- this file exists to prove tokens stay OUT
# of the log, so it must not put a credential-shaped one into the repo.
SHARE_TOKEN = "lhshr_0000.not-a-real-share-token-for-tests-only"


def test_access_log_redacts_the_share_token_from_every_path_that_carries_it(caplog):
    client = TestClient(app)

    with caplog.at_level(logging.INFO, logger=ACCESS_LOGGER):
        client.get(f"/api/public/session-shares/{SHARE_TOKEN}/preview")
        client.get(f"/api/timeline/session-shares/{SHARE_TOKEN}/resolve")
        # The share URL a recipient actually clicks: the SPA landing page
        # carries the same token in the same position.
        client.get(f"/share/{SHARE_TOKEN}")

    lines = [record.getMessage() for record in caplog.records if record.name == ACCESS_LOGGER]
    assert len(lines) >= 3, lines
    # The credential itself is gone, and the line still says which route was
    # read so the log keeps its forensic value.
    assert not any(SHARE_TOKEN in line for line in lines)
    assert not any(SHARE_TOKEN in str(record.__dict__) for record in caplog.records)
    assert any(line.startswith("GET /api/public/session-shares/[redacted]/preview ") for line in lines)
    assert any(line.startswith("GET /api/timeline/session-shares/[redacted]/resolve ") for line in lines)
    assert any(line.startswith("GET /share/[redacted] ") for line in lines)


def test_redaction_survives_the_routes_being_unmounted(caplog):
    """The guard is about the log line, not about the request succeeding.

    Every share path below is now unrouted, so each of these is a 404 or an SPA
    fallback. The token must be gone from the log either way -- a failed
    request is exactly when a mistyped or leaked token shows up in a URL.
    """

    client = TestClient(app)

    with caplog.at_level(logging.INFO, logger=ACCESS_LOGGER):
        preview = client.get(f"/api/public/session-shares/{SHARE_TOKEN}/preview")
        resolve = client.get(f"/api/timeline/session-shares/{SHARE_TOKEN}/resolve")

    assert preview.status_code == 404, preview.text
    assert resolve.status_code == 404, resolve.text
    lines = [record.getMessage() for record in caplog.records if record.name == ACCESS_LOGGER]
    assert any(line.endswith("404") for line in lines), lines
    assert not any(SHARE_TOKEN in line for line in lines)
