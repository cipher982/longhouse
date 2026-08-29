"""Real-catalog Runtime Host entrypoint for the browser test suite.

The browser suite gets a fresh file-backed SQLite root from
``spawn-test-backend.js``.  Import the normal application only after confirming
the process is test-scoped; the shared lifespan recognizes ``test:e2e`` and
starts the same catalog owner and storage lanes as a Runtime Host without its
unrelated production background loops.
"""

from zerg import config as _config

_settings = _config.get_settings_unchecked()
if not _settings.testing:
    raise RuntimeError("zerg.qa.e2e_app is test-only")
from zerg.main import app  # noqa: E402

__all__ = ["app"]
