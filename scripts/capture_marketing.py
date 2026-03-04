#!/usr/bin/env python3
"""
Marketing screenshot capture.

Reads manifest, navigates to URLs, waits for ready signal, screenshots.
No clicking, no CSS injection, no arbitrary waits.

Usage:
    uv run scripts/capture_marketing.py                       # Capture all
    uv run scripts/capture_marketing.py --name chat-preview   # Capture one
    uv run scripts/capture_marketing.py --list                # List available
    uv run scripts/capture_marketing.py --validate            # Check outputs exist
"""

import argparse
import json
import sys
import urllib.request
from pathlib import Path

import yaml

MANIFEST_PATH = Path(__file__).parent / "screenshots.yaml"
FRONTEND_DIR = Path(__file__).parent.parent / "apps" / "zerg" / "frontend-web"
READY_TIMEOUT = 15000  # 15 seconds


def load_manifest():
    with open(MANIFEST_PATH) as f:
        return yaml.safe_load(f)


def resolve_url_templates(url: str, base_url: str) -> str:
    """Replace {featured_session_id} / {first_session_id} placeholders.

    {featured_session_id} picks the completed session with the most tool_calls
    — better for marketing as it shows a dense, interesting event timeline.
    """
    api_url = base_url.replace("://localhost:47200", "://localhost:47300")

    if "{featured_session_id}" in url:
        try:
            resp = urllib.request.urlopen(f"{api_url}/api/agents/sessions?days_back=30&limit=50")
            sessions = json.loads(resp.read()).get("sessions", [])
            best = max(
                (s for s in sessions if s.get("ended_at")),
                key=lambda s: s.get("tool_calls", 0),
                default=sessions[0] if sessions else None,
            )
            if best:
                return url.replace("{featured_session_id}", best["id"])
        except Exception as e:
            print(f"  Warning: Could not resolve featured session ID: {e}")
        return url.replace("{featured_session_id}", "")

    if "{first_session_id}" not in url:
        return url
    try:
        resp = urllib.request.urlopen(f"{api_url}/api/agents/sessions?days_back=30&limit=1")
        data = json.loads(resp.read())
        session_id = data["sessions"][0]["id"]
        return url.replace("{first_session_id}", session_id)
    except Exception as e:
        print(f"  Warning: Could not resolve session ID: {e}")
        return url


def capture_screenshot(browser, name: str, config: dict, base_url: str):
    """Capture a single screenshot."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeout  # noqa: PLC0415

    page = browser.new_page(
        viewport={"width": config["viewport"]["width"], "height": config["viewport"]["height"]}
    )

    resolved_path = resolve_url_templates(config["url"], base_url)
    url = f"{base_url}{resolved_path}"
    print(f"  Navigating to {resolved_path}")
    page.goto(url)

    # Wait for app to signal screenshot readiness (content loaded, animations settled)
    try:
        page.wait_for_selector("[data-screenshot-ready='true']", timeout=READY_TIMEOUT)
    except PlaywrightTimeout:
        print(f"  Warning: Screenshot-ready signal not received for {name}, capturing anyway")

    # Build screenshot args
    output_path = FRONTEND_DIR / config["output"]
    output_path.parent.mkdir(parents=True, exist_ok=True)

    screenshot_args = {"path": str(output_path)}
    if "crop" in config:
        screenshot_args["clip"] = config["crop"]

    page.screenshot(**screenshot_args)

    size_kb = output_path.stat().st_size / 1024
    print(f"  {name} ({size_kb:.0f} KB)")

    page.close()


def capture_all(manifest: dict, names: list[str] | None = None):
    """Capture screenshots."""
    from playwright.sync_api import sync_playwright  # noqa: PLC0415

    base_url = manifest["base_url"]
    screenshots = manifest["screenshots"]

    if names:
        screenshots = {k: v for k, v in screenshots.items() if k in names}
        if not screenshots:
            print(f"No screenshots found matching: {names}")
            return False

    print(f"\nCapturing {len(screenshots)} screenshots...\n")

    with sync_playwright() as p:
        browser = p.chromium.launch()

        for name, config in screenshots.items():
            capture_screenshot(browser, name, config, base_url)

        browser.close()

    print(f"\nDone! Captured {len(screenshots)} screenshots.\n")
    return True


def validate(manifest: dict):
    """Check all output files exist and have reasonable size."""
    print("\nValidating screenshots...\n")

    all_valid = True
    for name, config in manifest["screenshots"].items():
        output_path = FRONTEND_DIR / config["output"]

        if not output_path.exists():
            print(f"  {name}: MISSING")
            all_valid = False
            continue

        size_kb = output_path.stat().st_size / 1024
        if size_kb < 10:
            print(f"  {name}: TOO SMALL ({size_kb:.0f} KB)")
            all_valid = False
        elif size_kb > 2000:
            print(f"  {name}: LARGE ({size_kb:.0f} KB)")
        else:
            print(f"  {name}: OK ({size_kb:.0f} KB)")

    print()
    return all_valid


def list_screenshots(manifest: dict):
    """List available screenshots."""
    print("\nAvailable screenshots:\n")
    for name, config in manifest["screenshots"].items():
        print(f"  {name:20s} {config.get('description', '')}")
    print()


def main():
    parser = argparse.ArgumentParser(description="Capture marketing screenshots")
    parser.add_argument("--name", "-n", action="append", help="Capture specific screenshot(s)")
    parser.add_argument("--list", "-l", action="store_true", help="List available screenshots")
    parser.add_argument("--validate", "-v", action="store_true", help="Validate existing screenshots")
    parser.add_argument("--base-url", help="Override base URL from manifest (e.g. http://localhost:47200)")
    args = parser.parse_args()

    manifest = load_manifest()
    if args.base_url:
        manifest["base_url"] = args.base_url

    if args.list:
        list_screenshots(manifest)
        return 0

    if args.validate:
        return 0 if validate(manifest) else 1

    success = capture_all(manifest, args.name)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
