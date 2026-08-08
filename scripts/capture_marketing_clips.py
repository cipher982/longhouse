#!/usr/bin/env python3
"""
Marketing video clip capture.

Records short scripted interactions against the demo stack with Playwright's
video recorder, then encodes them to H.264 MP4 for the landing page. Companion
to capture_marketing.py — same stack, same readiness contract:

    ./scripts/marketing-screenshots.sh clips
    # or against an already-running demo stack:
    uv run --with playwright --with pyyaml scripts/capture_marketing_clips.py \
        --base-url http://localhost:47398
"""

import argparse
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from capture_marketing import resolve_url_templates

FRONTEND_DIR = Path(__file__).parent.parent / "web"
OUTPUT_DIR = FRONTEND_DIR / "public" / "videos"
VIEWPORT = {"width": 1400, "height": 900}
READY_TIMEOUT = 15000
# Same instinct as the <2MB web/public image gate: landing clips must stay
# cheap enough to autoplay three at once.
VIDEO_MAX_BYTES = 3_000_000


def _scroll(page, total_px: int, steps: int = 24, step_ms: int = 45):
    """Wheel-scroll smoothly so the recording shows motion, not a jump cut."""
    step = total_px // steps
    for _ in range(steps):
        page.mouse.wheel(0, step)
        page.wait_for_timeout(step_ms)


def clip_timeline(page):
    page.wait_for_timeout(1200)
    _scroll(page, 900)
    page.wait_for_timeout(900)
    _scroll(page, 700)
    page.wait_for_timeout(1400)


def clip_search(page):
    page.wait_for_timeout(1000)
    box = page.get_by_placeholder("Search sessions...")
    box.click()
    page.keyboard.type("timeline", delay=140)
    page.wait_for_timeout(2800)


def clip_session_detail(page):
    page.wait_for_timeout(1400)
    _scroll(page, 1200, steps=30)
    page.wait_for_timeout(1400)


CLIPS = {
    "timeline-clip": {
        "url": "/timeline?marketing=true",
        "expect": '[data-testid="timeline-inbox"]',
        "action": clip_timeline,
    },
    "search-clip": {
        "url": "/timeline?marketing=true",
        "expect": '[data-testid="timeline-inbox"]',
        "action": clip_search,
    },
    "session-detail-clip": {
        "url": "/timeline/{featured_session_id}?marketing=true",
        "expect": None,
        "action": clip_session_detail,
    },
}


def record_clip(browser, name: str, spec: dict, base_url: str, tmpdir: str) -> tuple[Path, float]:
    """Record one clip; returns (webm path, seconds to trim off the head)."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeout  # noqa: PLC0415

    context = browser.new_context(
        viewport=VIEWPORT,
        device_scale_factor=1,
        color_scheme="dark",
        locale="en-US",
        timezone_id="America/Los_Angeles",
        record_video_dir=tmpdir,
        record_video_size=VIEWPORT,
    )
    started = time.monotonic()
    try:
        page = context.new_page()
        resolved_path = resolve_url_templates(spec["url"], base_url)
        print(f"  Recording {name} at {resolved_path}")
        page.goto(f"{base_url}{resolved_path}")

        try:
            page.wait_for_selector("[data-screenshot-ready='true']", timeout=READY_TIMEOUT)
        except PlaywrightTimeout:
            print(f"  Warning: ready signal not received for {name}, recording anyway")

        # Hard gate, same as stills: an empty or error state must fail the run.
        if spec["expect"]:
            try:
                page.wait_for_selector(spec["expect"], timeout=READY_TIMEOUT)
            except PlaywrightTimeout as exc:
                raise RuntimeError(
                    f"{name}: expected selector '{spec['expect']}' never appeared "
                    f"at {resolved_path} — refusing to record a wrong/empty page"
                ) from exc

        trim_head = time.monotonic() - started
        spec["action"](page)
        video = page.video
    finally:
        context.close()  # flushes the webm to disk

    return Path(video.path()), trim_head


def encode(webm: Path, out_path: Path, trim_head: float):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-ss", f"{max(trim_head - 0.2, 0):.2f}",
            "-i", str(webm),
            "-an",
            "-c:v", "libx264",
            "-crf", "27",
            "-preset", "medium",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(out_path),
        ],
        check=True,
        capture_output=True,
    )
    size = out_path.stat().st_size
    if size > VIDEO_MAX_BYTES:
        raise RuntimeError(
            f"{out_path.name} is {size // 1024}KB, over the {VIDEO_MAX_BYTES // 1024}KB landing clip budget"
        )
    print(f"  {out_path.name} ({size // 1024} KB)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--name", help="Record a single clip by name")
    args = parser.parse_args()

    clips = CLIPS
    if args.name:
        if args.name not in CLIPS:
            print(f"Unknown clip '{args.name}'. Available: {', '.join(CLIPS)}")
            return 1
        clips = {args.name: CLIPS[args.name]}

    from playwright.sync_api import sync_playwright  # noqa: PLC0415

    print(f"\nRecording {len(clips)} clips...\n")
    with sync_playwright() as p, tempfile.TemporaryDirectory() as tmpdir:
        browser = p.chromium.launch()
        for name, spec in clips.items():
            webm, trim_head = record_clip(browser, name, spec, args.base_url, tmpdir)
            encode(webm, OUTPUT_DIR / f"{name}.mp4", trim_head)
        browser.close()

    print(f"\nDone! Recorded {len(clips)} clips.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
