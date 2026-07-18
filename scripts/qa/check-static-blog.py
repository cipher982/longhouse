#!/usr/bin/env python3
"""Small, dependency-free checks for the independently deployed static blog."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2] / "blog"
PAGES = (ROOT / "index.html", ROOT / "provider-integrations" / "index.html")


class Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"a", "link", "script"}:
            for name, value in attrs:
                if name in {"href", "src"} and value:
                    self.hrefs.append(value)


def main() -> int:
    errors: list[str] = []
    for page in PAGES:
        if not page.is_file():
            errors.append(f"missing page: {page.relative_to(ROOT)}")
            continue
        text = page.read_text(encoding="utf-8")
        if "localhost" in text:
            errors.append(f"localhost reference: {page.relative_to(ROOT)}")
        if page.name == "index.html" and page.parent.name == "provider-integrations" and "Guest post by <strong>Codex</strong>" not in text:
            errors.append("provider post is missing the explicit guest byline")
        parser = Links()
        parser.feed(text)
        for href in parser.hrefs:
            if href.startswith("/blog/") and not href.startswith("/blog/assets/"):
                relative = href.removeprefix("/blog/")
                target = ROOT / relative
                if href.endswith("/"):
                    target /= "index.html"
                if not target.is_file():
                    errors.append(f"broken local blog link in {page.relative_to(ROOT)}: {href}")
            if href.startswith("/blog/assets/") and not (ROOT / href.removeprefix("/blog/")).is_file():
                errors.append(f"missing asset in {page.relative_to(ROOT)}: {href}")
    if errors:
        print("Static blog validation failed:", *[f"- {error}" for error in errors], sep="\n")
        return 1
    print(f"Static blog validation passed ({len(PAGES)} pages).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
