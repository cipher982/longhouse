"""Real-catalog Runtime Host entrypoint for the browser test suite.

The browser suite gets a fresh file-backed SQLite root from
``spawn-test-backend.js``.  Import the normal application only after confirming
the process is test-scoped; the shared lifespan recognizes ``test:e2e`` and
starts the same catalog owner and storage lanes as a Runtime Host without its
unrelated production background loops.
"""

import os

# Same truthy set as zerg.config.Settings.testing (TESTING env), read directly so the
# verifier entrypoint does not import the config module it judges.
if os.getenv("TESTING", "").strip().lower() not in {"1", "true", "yes", "on"}:
    raise RuntimeError("zerg.qa.e2e_app is test-only")
from zerg.main import app  # noqa: E402

__all__ = ["app"]
