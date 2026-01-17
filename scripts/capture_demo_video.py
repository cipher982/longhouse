#!/usr/bin/env python3
"""
Capture product demo video with audio-driven timing.

PREREQUISITE: Run `uv run scripts/generate_voiceover.py product-demo` first!

Usage:
    uv run scripts/capture_demo_video.py product-demo
    uv run scripts/capture_demo_video.py product-demo --scene chat-briefing
    uv run scripts/capture_demo_video.py --list
"""

import argparse
import json
import logging
from pathlib import Path

import yaml
from playwright.sync_api import Page, sync_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

SCENARIO_DIR = Path(__file__).parent / "video-scenarios"
BASE_URL = "http://localhost:30080"


def load_scenario(name: str) -> dict:
    """Load scenario YAML file."""
    path = SCENARIO_DIR / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Scenario not found: {path}")
    with open(path) as f:
        return yaml.safe_load(f)


def load_audio_durations(scenario_name: str) -> dict[str, float]:
    """Load pre-computed audio durations."""
    path = Path("videos") / scenario_name / "audio" / "durations.json"
    if not path.exists():
        logger.warning(f"Audio durations not found at {path}. Using fallback timing.")
        return {}
    with open(path) as f:
        return json.load(f)


def inject_click_indicator(page: Page) -> None:
    """Add visual ripple effect on clicks for video recording."""
    page.evaluate("""
        // Only inject once
        if (window.__clickIndicatorInjected) return;
        window.__clickIndicatorInjected = true;

        document.addEventListener('click', (e) => {
            const ripple = document.createElement('div');
            ripple.className = 'click-indicator';
            ripple.style.cssText = `
                position: fixed;
                width: 40px; height: 40px;
                left: ${e.clientX - 20}px;
                top: ${e.clientY - 20}px;
                border-radius: 50%;
                background: rgba(99, 102, 241, 0.4);
                pointer-events: none;
                animation: click-ripple 0.6s ease-out forwards;
                z-index: 99999;
            `;
            document.body.appendChild(ripple);
            setTimeout(() => ripple.remove(), 600);
        });

        // Add animation keyframes if not already present
        if (!document.getElementById('click-ripple-style')) {
            const style = document.createElement('style');
            style.id = 'click-ripple-style';
            style.textContent = `
                @keyframes click-ripple {
                    0% { transform: scale(0); opacity: 1; }
                    100% { transform: scale(2); opacity: 0; }
                }
            `;
            document.head.appendChild(style);
        }
    """)


def execute_step(page: Page, step: dict, audio_duration: float | None) -> None:
    """Execute a single scenario step."""
    action = step["action"]

    if action == "navigate":
        url = step["url"]
        full_url = f"{BASE_URL}{url}" if url.startswith("/") else url
        logger.info(f"  Navigating to: {full_url}")
        page.goto(full_url)

        # Inject click indicator after navigation
        inject_click_indicator(page)

        if wait_for := step.get("wait_for"):
            logger.info(f"  Waiting for: {wait_for}")
            page.wait_for_selector(wait_for, timeout=15000)

    elif action == "type":
        selector = step["selector"]
        text = step["text"]
        delay = step.get("delay_ms", 50)
        logger.info(f"  Typing into {selector}: {text[:30]}...")
        page.type(selector, text, delay=delay)

    elif action == "click":
        selector = step["selector"]
        logger.info(f"  Clicking: {selector}")
        page.click(selector)

    elif action == "wait":
        selector = step["selector"]
        timeout = step.get("timeout_ms", 30000)
        logger.info(f"  Waiting for: {selector}")
        page.wait_for_selector(selector, timeout=timeout)

    elif action == "pause":
        duration_ms = step["duration_ms"]
        logger.info(f"  Pausing for {duration_ms}ms")
        page.wait_for_timeout(duration_ms)

    elif action == "pause_for_audio":
        # KEY: Use actual audio duration + buffer
        if audio_duration:
            wait_ms = int((audio_duration + 0.5) * 1000)
            logger.info(f"  Pausing for audio: {audio_duration:.1f}s + 0.5s buffer = {wait_ms}ms")
            page.wait_for_timeout(wait_ms)
        else:
            # Fallback when no audio
            logger.info("  No audio duration, using 3s fallback")
            page.wait_for_timeout(3000)

    elif action == "scroll":
        direction = step.get("direction", "down")
        amount = step.get("amount", 300)
        delta = amount if direction == "down" else -amount
        logger.info(f"  Scrolling {direction} by {amount}px")
        page.mouse.wheel(0, delta)

    else:
        logger.warning(f"  Unknown action: {action}")


def record_scene(
    browser,
    scene: dict,
    audio_duration: float | None,
    output_dir: Path,
    viewport: dict,
) -> Path:
    """Record a single scene."""
    scene_id = scene["id"]
    logger.info(f"Recording scene: {scene_id}")

    context = browser.new_context(
        record_video_dir=str(output_dir / "raw"),
        record_video_size=viewport,
        viewport=viewport,
    )
    page = context.new_page()

    # Inject click indicator at start
    inject_click_indicator(page)

    if audio_duration:
        logger.info(f"  Audio duration: {audio_duration:.1f}s")
    else:
        logger.info("  No audio for this scene")

    for step in scene["steps"]:
        execute_step(page, step, audio_duration)

    # Small buffer at end
    page.wait_for_timeout(500)

    # Close context to finalize video
    context.close()

    # Get the video path (Playwright saves with random name)
    video_path = page.video.path()

    # Rename to scene ID
    if video_path:
        video_path = Path(video_path)
        final_path = output_dir / f"{scene_id}.webm"
        video_path.rename(final_path)
        logger.info(f"  Saved: {final_path}")
        return final_path
    else:
        logger.error(f"  No video recorded for scene: {scene_id}")
        return None


def record_scenario(scenario_name: str, scene_filter: str | None = None) -> list[Path]:
    """Record all scenes with audio-driven timing."""
    scenario = load_scenario(scenario_name)
    audio_durations = load_audio_durations(scenario_name)

    output_dir = Path("videos") / scenario_name
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "raw").mkdir(exist_ok=True)

    scenes = scenario["scenes"]
    if scene_filter:
        scenes = [s for s in scenes if s["id"] == scene_filter]
        if not scenes:
            raise ValueError(f"Scene not found: {scene_filter}")

    logger.info(f"Recording {len(scenes)} scene(s) for scenario: {scenario_name}")

    viewport = {"width": 1920, "height": 1080}
    video_paths = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)  # Visible for debugging

        for scene in scenes:
            audio_dur = audio_durations.get(scene["id"])
            path = record_scene(browser, scene, audio_dur, output_dir, viewport)
            if path:
                video_paths.append(path)

        browser.close()

    # Generate ffmpeg manifest
    manifest_path = output_dir / "scenes.txt"
    with open(manifest_path, "w") as f:
        for path in video_paths:
            f.write(f"file '{path.name}'\n")

    logger.info(f"Recorded {len(video_paths)} videos")
    logger.info(f"Manifest: {manifest_path}")
    return video_paths


def list_scenarios() -> None:
    """List available scenarios."""
    print("Available scenarios:")
    for f in SCENARIO_DIR.glob("*.yaml"):
        scenario = load_scenario(f.stem)
        scene_count = len(scenario.get("scenes", []))
        print(f"  - {f.stem} ({scene_count} scenes)")

        # List scene IDs
        for scene in scenario.get("scenes", []):
            voiceover = "+" if scene.get("voiceover") else "-"
            print(f"      {voiceover} {scene['id']}")


def main():
    parser = argparse.ArgumentParser(description="Capture product demo video")
    parser.add_argument("scenario", default="product-demo", nargs="?", help="Scenario name (default: product-demo)")
    parser.add_argument("--scene", help="Record specific scene only")
    parser.add_argument("--list", action="store_true", help="List available scenarios")
    args = parser.parse_args()

    if args.list:
        list_scenarios()
        return

    record_scenario(args.scenario, args.scene)


if __name__ == "__main__":
    main()
