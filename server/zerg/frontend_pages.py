"""Prerendered HTML for the public site's marketing routes.

`web/scripts/prerender.mjs` writes `dist/_prerender/<route>/index.html` for every
page in the sitemap: the real page markup plus its own title, description,
canonical and OpenGraph tags, so crawlers and link unfurlers that do not run
JavaScript see content. The browser hydrates that DOM.

Only the public site gets them. The pages are rendered with the demo site's
config, so the markup matches what a demo-mode browser builds (and what
hydration expects); any other Runtime Host keeps serving the SPA shell.
"""

from __future__ import annotations

from pathlib import Path

from zerg.config import AppMode

PRERENDER_DIR_NAME = "_prerender"


def prerendered_page(dist_dir: Path, path: str, *, app_mode: AppMode) -> Path | None:
    """The prerendered HTML file for a request path, or None to serve the shell."""
    if app_mode is not AppMode.DEMO:
        return None
    root = dist_dir / PRERENDER_DIR_NAME
    route = path.strip("/")
    try:
        page = (root / route / "index.html").resolve()
        if page.is_relative_to(root.resolve()) and page.is_file():
            return page
    except (ValueError, OSError):
        pass
    return None
