"""Single-store Runtime Host entrypoint for the browser test suite.

The browser fixtures create auth, seed sessions, and reset data in one isolated
SQLite database per Playwright worker. Select that test topology before the
real app imports database routing; normal Runtime Hosts retain the storage-v2
split and archive helpers retain live-catalog authentication.
"""

from zerg import config as _config

_settings = _config.get_settings_unchecked()
if not _settings.testing:
    raise RuntimeError("zerg.qa.e2e_app is test-only")


def _single_store_url(_database_url: str) -> str:
    return ""


_config.resolve_live_database_url = _single_store_url

from zerg.main import app  # noqa: E402

__all__ = ["app"]
