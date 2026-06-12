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
FRONTEND_DIR = Path(__file__).parent.parent / "web"
READY_TIMEOUT = 15000  # 15 seconds
TIMELINE_SESSIONS_PATH = "/api/timeline/sessions"


def load_manifest():
    with open(MANIFEST_PATH) as f:
        return yaml.safe_load(f)


def load_timeline_sessions(api_url: str, limit: int) -> list[dict]:
    """Fetch browser-owned timeline sessions for placeholder resolution."""
    resp = urllib.request.urlopen(f"{api_url}{TIMELINE_SESSIONS_PATH}?days_back=30&limit={limit}")
    payload = json.loads(resp.read())

    if isinstance(payload, dict):
        sessions = payload.get("sessions", [])
    elif isinstance(payload, list):
        sessions = payload
    else:
        raise ValueError("unexpected sessions payload")

    if not isinstance(sessions, list):
        raise ValueError("sessions payload missing 'sessions' list")

    return sessions


def _session_id(session: dict):
    """Extract the id that the /timeline/:sessionId detail route expects.

    A timeline card's `thread_id` is the THREAD id, not the session id the
    detail page loads by. The frontend navigates with `detail.id` (the head
    session of the thread) — mirror that, then fall back to flatter shapes.
    """
    detail = session.get("detail")
    if isinstance(detail, dict) and detail.get("id") not in (None, ""):
        return detail["id"]
    for key in ("head_session_id", "session_id", "id", "thread_id"):
        value = session.get(key)
        if value not in (None, ""):
            return value
    raise KeyError("no session id field (detail.id/head_session_id/session_id/id) in payload")


def _tool_call_count(session: dict) -> int:
    """Tool-call count whether the field is a list of calls or a number.

    Looks at the card's `detail` (an AgentSession) first, then the card itself.
    """
    detail = session.get("detail")
    value = (detail or {}).get("tool_calls") if isinstance(detail, dict) else None
    if value is None:
        value = session.get("tool_calls", 0)
    if isinstance(value, list):
        return len(value)
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def _is_ended(session: dict) -> bool:
    """True if the session is completed (ended_at set), tolerant of card shape."""
    detail = session.get("detail")
    if isinstance(detail, dict) and detail.get("ended_at"):
        return True
    return bool(session.get("ended_at"))


def resolve_url_templates(url: str, base_url: str) -> str:
    """Replace {featured_session_id} / {first_session_id} placeholders.

    {featured_session_id} picks the completed session with the most tool_calls
    — better for marketing as it shows a dense, interesting event timeline.
    """
    # Screenshot base URLs are UI origins with /api available via the same host
    # (Vite proxy in dev, nginx in demo/prod-like capture flows).
    api_url = base_url

    if "{featured_session_id}" in url:
        try:
            sessions = load_timeline_sessions(api_url, limit=50)
            best = max(
                (s for s in sessions if _is_ended(s)),
                key=_tool_call_count,
                default=sessions[0] if sessions else None,
            )
            if best:
                return url.replace("{featured_session_id}", str(_session_id(best)))
        except Exception as e:
            print(f"  Warning: Could not resolve featured session ID: {e}")
        return url.replace("{featured_session_id}", "")

    if "{first_session_id}" not in url:
        return url
    try:
        sessions = load_timeline_sessions(api_url, limit=1)
        return url.replace("{first_session_id}", str(_session_id(sessions[0])))
    except Exception as e:
        print(f"  Warning: Could not resolve session ID: {e}")
        return url


# CSS injected before capture to kill animations/transitions so screenshots are
# deterministic regardless of when the ready signal fires (mirrors ui-capture.ts).
_ANIMATION_KILL_CSS = """
  *, *::before, *::after {
    transition: none !important;
    animation: none !important;
    animation-delay: 0s !important;
    animation-duration: 0s !important;
    caret-color: transparent !important;
  }
"""

# Default capture knobs — overridable per-entry in the manifest.
DEFAULT_SCALE = 2  # retina; raw 1x output looked soft on the landing page
DEFAULT_COLOR_SCHEME = "dark"


def _append_query(path: str, extra: str) -> str:
    """Append a query param to a path, respecting any existing query string."""
    sep = "&" if "?" in path else "?"
    return f"{path}{sep}{extra}"


# Retina PNGs are large; the web/public image CI gate rejects files >2MB.
_IMAGE_MAX_BYTES = 2_000_000


def _optimize_png(path: Path) -> None:
    """Quantize a PNG to satisfy the <2MB web/public CI gate.

    Quantizes to a temp file and only replaces the original on success, so a
    pngquant failure (or its non-zero --skip-if-larger exit) can never corrupt
    or truncate the captured screenshot. Hard-fails if the final image still
    exceeds the gate — an oversized asset must fail the run loudly, not ship
    and break CI later.
    """
    import shutil  # noqa: PLC0415
    import subprocess  # noqa: PLC0415

    pngquant = shutil.which("pngquant")
    if pngquant:
        tmp = path.with_name(path.name + ".opt")
        result = subprocess.run(
            [pngquant, "--force", "--skip-if-larger", "--quality=70-95", "--output", str(tmp), str(path)],
            capture_output=True,
        )
        # Exit 0 => a smaller PNG was written; adopt it. Any non-zero exit
        # (--skip-if-larger skip, quality floor, or a real error) leaves the
        # original untouched. Only trust a non-empty temp file.
        if result.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
            tmp.replace(path)
        else:
            tmp.unlink(missing_ok=True)

    size = path.stat().st_size
    if size > _IMAGE_MAX_BYTES:
        hint = "" if pngquant else " — install pngquant (`brew install pngquant`)"
        raise RuntimeError(
            f"{path.name} is {size // 1024}KB, over the {_IMAGE_MAX_BYTES // 1024}KB web/public image gate{hint}"
        )


def capture_screenshot(browser, name: str, config: dict, base_url: str):
    """Capture a single screenshot."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeout  # noqa: PLC0415

    # Deterministic, launch-grade context: retina scale factor, frozen motion,
    # fixed locale/timezone/theme so captures are byte-stable run-to-run.
    context = browser.new_context(
        viewport={"width": config["viewport"]["width"], "height": config["viewport"]["height"]},
        device_scale_factor=config.get("scale", DEFAULT_SCALE),
        color_scheme=config.get("color_scheme", DEFAULT_COLOR_SCHEME),
        reduced_motion="reduce",
        locale="en-US",
        timezone_id="America/Los_Angeles",
    )
    try:
        page = context.new_page()

        resolved_path = resolve_url_templates(config["url"], base_url)
        # Freeze the in-app clock (Apple-style 9:41) unless the entry opts out, so
        # relative timestamps ("2h ago") don't drift between runs.
        if config.get("clock", "frozen") == "frozen" and "clock=" not in resolved_path:
            resolved_path = _append_query(resolved_path, "clock=frozen")
        url = f"{base_url}{resolved_path}"
        print(f"  Navigating to {resolved_path}")
        page.goto(url)

        # Wait for app to signal screenshot readiness (content loaded, animations settled)
        try:
            page.wait_for_selector("[data-screenshot-ready='true']", timeout=READY_TIMEOUT)
        except PlaywrightTimeout:
            print(f"  Warning: Screenshot-ready signal not received for {name}, capturing anyway")

        # Assert the page reached a real (non-empty) marketing state. This is a
        # HARD gate: capturing an empty-state or error page that merely renders is
        # the worst failure for a marketing pipeline, so a missing expect_selector
        # fails the run instead of silently shipping a wrong-but-non-empty image.
        expect_selector = config.get("expect_selector")
        if expect_selector:
            try:
                page.wait_for_selector(expect_selector, timeout=READY_TIMEOUT)
            except PlaywrightTimeout as exc:
                raise RuntimeError(
                    f"{name}: expected selector '{expect_selector}' never appeared at {resolved_path} "
                    f"— refusing to capture a wrong/empty page"
                ) from exc

        # Kill animations + settle briefly so transient motion never lands in a frame.
        page.add_style_tag(content=_ANIMATION_KILL_CSS)
        page.wait_for_timeout(150)

        # Build screenshot args
        output_path = FRONTEND_DIR / config["output"]
        output_path.parent.mkdir(parents=True, exist_ok=True)

        screenshot_args = {"path": str(output_path)}
        if "crop" in config:
            screenshot_args["clip"] = config["crop"]

        page.screenshot(**screenshot_args)
        _optimize_png(output_path)

        size_kb = output_path.stat().st_size / 1024
        print(f"  {name} ({size_kb:.0f} KB)")
    finally:
        context.close()


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
