"""A demo corpus's default listing must pass catalogd's own validation.

The first cut passed days_back=None, which catalogd rejects (1..3650), so
longhouse.ai's timeline answered 503 instead of its sessions (2026-10-07).
"""

from __future__ import annotations

import pytest

pytest_plugins = ("tests_lite.live_catalog_harness",)


@pytest.mark.parametrize("flag", ["LONGHOUSE_DEMO_CORPUS", None])
def test_default_listing_and_filters_pass_catalogd_validation(live_catalog, monkeypatch, flag):
    if flag:
        monkeypatch.setenv(flag, "1")
    owner_id = live_catalog.create_user("demo-listing@example.test")
    cookie = live_catalog.browser_cookie(owner_id=owner_id, email="demo-listing@example.test")
    with live_catalog.http_client() as client:
        listing = client.get("/timeline/sessions", params={"limit": 3}, cookies={"longhouse_session": cookie})
        filters = client.get("/timeline/filters", cookies={"longhouse_session": cookie})
    assert listing.status_code == 200, listing.text
    assert filters.status_code == 200, filters.text
