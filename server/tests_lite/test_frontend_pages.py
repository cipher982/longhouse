from __future__ import annotations

from pathlib import Path

from zerg.config import AppMode
from zerg.frontend_pages import prerendered_page


def _dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    (dist / "_prerender" / "docs" / "quickstart").mkdir(parents=True)
    (dist / "index.html").write_text("shell")
    (dist / "_prerender" / "index.html").write_text("landing")
    (dist / "_prerender" / "docs" / "index.html").write_text("docs")
    (dist / "_prerender" / "docs" / "quickstart" / "index.html").write_text("quickstart")
    return dist


def test_public_site_serves_the_prerendered_page_for_a_route(tmp_path):
    dist = _dist(tmp_path)

    assert prerendered_page(dist, "/", app_mode=AppMode.DEMO) == dist / "_prerender" / "index.html"
    assert prerendered_page(dist, "docs", app_mode=AppMode.DEMO) == dist / "_prerender" / "docs" / "index.html"
    assert prerendered_page(dist, "docs/quickstart/", app_mode=AppMode.DEMO) == (dist / "_prerender" / "docs" / "quickstart" / "index.html")


def test_only_the_public_site_gets_prerendered_pages(tmp_path):
    dist = _dist(tmp_path)

    for mode in (AppMode.PRODUCTION, AppMode.DEV):
        assert prerendered_page(dist, "/", app_mode=mode) is None
        assert prerendered_page(dist, "docs", app_mode=mode) is None


def test_routes_without_a_prerendered_page_fall_back_to_the_shell(tmp_path):
    dist = _dist(tmp_path)

    for path in ("timeline", "docs/nope", "login", "_prerender", "_prerender/docs"):
        assert prerendered_page(dist, path, app_mode=AppMode.DEMO) is None


def test_request_paths_cannot_leave_the_prerender_directory(tmp_path):
    dist = _dist(tmp_path)
    (tmp_path / "secret").mkdir()
    (tmp_path / "secret" / "index.html").write_text("secret")

    assert prerendered_page(dist, "../secret", app_mode=AppMode.DEMO) is None
    assert prerendered_page(dist, "docs/../../../secret", app_mode=AppMode.DEMO) is None
    assert prerendered_page(dist, "//etc/passwd", app_mode=AppMode.DEMO) is None
    assert prerendered_page(dist, "docs\x00", app_mode=AppMode.DEMO) is None


def test_a_dist_without_prerendered_pages_serves_the_shell(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("shell")

    assert prerendered_page(dist, "/", app_mode=AppMode.DEMO) is None
