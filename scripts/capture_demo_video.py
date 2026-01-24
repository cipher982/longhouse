#!/usr/bin/env python3
"""
Capture product demo video with audio-driven timing.

Default recording mode is headless frame capture (screenshots → ffmpeg ProRes).
This produces raw high-quality video without needing a visible browser window.

Recording modes:
  - frames (default): Headless CDP screenshots piped to ffmpeg. High quality, no GUI needed.
  - screen: macOS screen capture. Requires visible browser window.
  - playwright: Playwright's built-in recorder. Low quality (VP8).
  - none: Drive browser without recording (for testing).

PREREQUISITE: Run `uv run scripts/generate_voiceover.py product-demo` first!

Usage:
    # Headless high-quality (default)
    uv run scripts/capture_demo_video.py product-demo --headless

    # Single scene test
    uv run scripts/capture_demo_video.py product-demo --headless --scene dashboard-intro

    # Screen capture (requires visible window)
    uv run scripts/capture_demo_video.py product-demo --record-mode screen

    # List scenarios
    uv run scripts/capture_demo_video.py --list
"""

import argparse
import json
import logging
import platform
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

import yaml
from playwright.sync_api import Page, sync_playwright, TimeoutError as PlaywrightTimeoutError

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

SCENARIO_DIR = Path(__file__).parent / "video-scenarios"
BASE_URL = "http://localhost:30080"
VIEWPORT = {"width": 1920, "height": 1080}


# -----------------------------------------------------------------------------
# Scenario helpers
# -----------------------------------------------------------------------------

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
    page.evaluate(
        """(() => {
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
    })()"""
    )


def execute_step(
    page: Page,
    step: dict,
    audio_duration: float | None,
    frame_recorder: "FrameCaptureRecorder | None" = None,
) -> None:
    """Execute a single scenario step.

    If frame_recorder is provided, captures frames during waits/pauses.
    """
    action = step["action"]

    def wait_with_frames(duration_ms: int) -> None:
        """Wait while capturing frames if recorder is active."""
        if frame_recorder:
            frame_recorder.capture_for_duration(duration_ms)
        else:
            page.wait_for_timeout(duration_ms)

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

        # Capture a few frames after navigation settles
        if frame_recorder:
            wait_with_frames(200)

    elif action == "type":
        selector = step["selector"]
        text = step["text"]
        delay = step.get("delay_ms", 50)
        logger.info(f"  Typing into {selector}: {text[:30]}...")

        if frame_recorder:
            # Capture frames while typing (align delays to video time)
            per_char_delay = max(1, int(delay))
            for char in text:
                page.type(selector, char, delay=0)
                frame_recorder.capture_for_duration(per_char_delay)
        else:
            page.type(selector, text, delay=delay)

    elif action == "click":
        selector = step["selector"]
        logger.info(f"  Clicking: {selector}")
        if frame_recorder:
            frame_recorder.capture_frame()  # Before click
        page.click(selector)
        if frame_recorder:
            wait_with_frames(100)  # Brief pause after click to show result

    elif action == "wait":
        selector = step["selector"]
        timeout = step.get("timeout_ms", 30000)
        optional = step.get("optional", False)
        logger.info(f"  Waiting for: {selector}")
        try:
            page.wait_for_selector(selector, timeout=timeout)
        except PlaywrightTimeoutError:
            if optional:
                logger.warning(f"  Optional wait timed out: {selector}")
            else:
                raise
        if frame_recorder:
            frame_recorder.capture_frame()

    elif action == "pause":
        duration_ms = step["duration_ms"]
        logger.info(f"  Pausing for {duration_ms}ms")
        wait_with_frames(duration_ms)

    elif action == "pause_for_audio":
        # KEY: Use actual audio duration + buffer
        if audio_duration:
            wait_ms = int((audio_duration + 0.5) * 1000)
            logger.info(f"  Pausing for audio: {audio_duration:.1f}s + 0.5s buffer = {wait_ms}ms")
            wait_with_frames(wait_ms)
        else:
            # Fallback when no audio
            logger.info("  No audio duration, using 3s fallback")
            wait_with_frames(3000)

    elif action == "scroll":
        direction = step.get("direction", "down")
        amount = step.get("amount", 300)
        delta = amount if direction == "down" else -amount
        logger.info(f"  Scrolling {direction} by {amount}px")
        if frame_recorder:
            frame_recorder.capture_frame()  # Before scroll
        page.mouse.wheel(0, delta)
        if frame_recorder:
            wait_with_frames(300)  # Capture smooth scroll

    else:
        logger.warning(f"  Unknown action: {action}")


# -----------------------------------------------------------------------------
# Screen recording helpers (macOS + ffmpeg)
# -----------------------------------------------------------------------------

def _find_default_screen_device() -> str | None:
    """Return the first AVFoundation screen device index (as string)."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None

    output = (result.stderr or "") + (result.stdout or "")
    for line in output.splitlines():
        match = re.search(r"\[(\d+)\]\s+Capture screen", line)
        if match:
            return match.group(1)
    return None


def _compute_crop_filter(page: Page) -> str | None:
    """Compute ffmpeg crop filter for the browser window (screen recording)."""
    try:
        bounds = page.evaluate(
            """() => {
            const dpr = window.devicePixelRatio || 1;
            const x = window.screenX;
            const y = window.screenY;
            return { x, y, w: window.outerWidth, h: window.outerHeight, dpr };
        }"""
        )
    except Exception:
        return None

    if not bounds:
        return None

    dpr = bounds.get("dpr", 1) or 1
    x = max(0, int(bounds.get("x", 0) * dpr))
    y = max(0, int(bounds.get("y", 0) * dpr))
    w = int(bounds.get("w", 0) * dpr)
    h = int(bounds.get("h", 0) * dpr)

    # Ensure even dimensions for H.264
    if w % 2 != 0:
        w -= 1
    if h % 2 != 0:
        h -= 1

    if w <= 0 or h <= 0:
        return None

    return f"crop={w}:{h}:{x}:{y}"


def _start_screen_recording(output_path: Path, fps: int, crf: int, screen_index: str | None, crop_filter: str | None):
    """Start ffmpeg screen recording (macOS only)."""
    if platform.system() != "Darwin":
        raise RuntimeError("Screen recording is only supported on macOS (avfoundation).")

    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg not found. Install with: brew install ffmpeg")

    device = screen_index or _find_default_screen_device()
    if not device:
        raise RuntimeError("No screen capture device found. Run: ffmpeg -f avfoundation -list_devices true -i \"\"")

    logger.info(f"  Screen capture device: {device} | fps: {fps} | crf: {crf}")

    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "avfoundation",
        "-framerate",
        str(fps),
        "-i",
        device,
    ]

    if crop_filter:
        cmd += ["-vf", crop_filter]

    cmd += [
        "-c:v",
        "libx264",
        "-preset",
        "slow",
        "-crf",
        str(crf),
        "-pix_fmt",
        "yuv420p",
        str(output_path),
    ]

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    # Give ffmpeg a moment to initialize
    time.sleep(0.2)
    return proc


def _stop_screen_recording(proc: subprocess.Popen | None) -> None:
    """Stop ffmpeg screen recording."""
    if not proc:
        return

    if proc.poll() is None:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

    if proc.returncode not in (0, None):
        stderr = ""
        try:
            stderr = proc.stderr.read().decode().strip() if proc.stderr else ""
        except Exception:
            stderr = ""
        if stderr:
            logger.error(f"ffmpeg error: {stderr}")


# -----------------------------------------------------------------------------
# Headless frame capture (CDP screenshots → ffmpeg)
# -----------------------------------------------------------------------------


class FrameCaptureRecorder:
    """Capture screenshots at target FPS and encode to ProRes.

    Always outputs raw ProRes 422 HQ - the source of truth.
    Compression/encoding for distribution is a separate pipeline step.

    Uses cooperative capture (main thread) instead of background thread because
    Playwright's sync API uses greenlets and isn't thread-safe.
    """

    def __init__(self, output_path: Path, fps: int, viewport: dict):
        self.output_path = output_path.with_suffix(".mov")
        self.fps = fps
        self.viewport = viewport
        self.page: Page | None = None
        self._ffmpeg_proc: subprocess.Popen | None = None
        self._frame_count = 0
        self._frame_interval = 1.0 / fps

    def start(self, page: Page) -> None:
        """Initialize ffmpeg process for receiving frames."""
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg not found. Install with: brew install ffmpeg")

        self.page = page
        self._frame_count = 0

        # Always output ProRes 422 HQ - raw source files
        cmd = [
            "ffmpeg",
            "-y",
            "-f", "image2pipe",
            "-framerate", str(self.fps),
            "-i", "-",
            "-c:v", "prores_ks",
            "-profile:v", "3",  # HQ profile
            "-pix_fmt", "yuv422p10le",
            "-vf", f"scale={self.viewport['width']}:{self.viewport['height']}:force_original_aspect_ratio=decrease,pad={self.viewport['width']}:{self.viewport['height']}:(ow-iw)/2:(oh-ih)/2",
            str(self.output_path),
        ]

        self._ffmpeg_proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        # Give ffmpeg a moment to initialize
        time.sleep(0.1)
        logger.info(f"  Recording: {self.fps} fps, ProRes 422 HQ")

    def capture_frame(self) -> None:
        """Capture a single frame (called from main thread)."""
        if not self.page or not self._ffmpeg_proc or not self._ffmpeg_proc.stdin:
            return

        try:
            png_data = self.page.screenshot(type="png")
            self._ffmpeg_proc.stdin.write(png_data)
            self._frame_count += 1
        except Exception as e:
            logger.warning(f"Frame capture error: {e}")

    def capture_for_duration(self, duration_ms: int) -> None:
        """Capture frames for a given duration at target FPS.

        This replaces page.wait_for_timeout() with frame-aware waiting.
        """
        if duration_ms <= 0:
            return

        duration_sec = duration_ms / 1000.0
        frames_needed = max(1, int(duration_sec * self.fps))

        for _ in range(frames_needed):
            start = time.monotonic()
            self.capture_frame()
            elapsed = time.monotonic() - start

            # Sleep remaining time in frame interval
            sleep_time = self._frame_interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

    def stop(self) -> None:
        """Finalize video encoding."""
        if self._ffmpeg_proc:
            if self._ffmpeg_proc.stdin:
                try:
                    self._ffmpeg_proc.stdin.close()
                except Exception:
                    pass

            try:
                self._ffmpeg_proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self._ffmpeg_proc.kill()
                self._ffmpeg_proc.wait(timeout=5)

            if self._ffmpeg_proc.returncode not in (0, None):
                stderr = ""
                try:
                    stderr = self._ffmpeg_proc.stderr.read().decode().strip() if self._ffmpeg_proc.stderr else ""
                except Exception:
                    pass
                if stderr:
                    logger.error(f"ffmpeg error: {stderr[:500]}")

        logger.info(f"  Captured {self._frame_count} frames")


# -----------------------------------------------------------------------------
# Recording
# -----------------------------------------------------------------------------

def record_scene(
    browser,
    scene: dict,
    audio_duration: float | None,
    output_dir: Path,
    record_mode: str,
    screen_index: str | None,
    fps: int,
    crf: int,
) -> Path | None:
    """Record a single scene."""
    scene_id = scene["id"]
    logger.info(f"Recording scene: {scene_id}")

    if record_mode == "playwright":
        context = browser.new_context(
            record_video_dir=str(output_dir / "raw"),
            record_video_size=VIEWPORT,
            viewport=VIEWPORT,
        )
    else:
        context = browser.new_context(
            viewport=VIEWPORT,
            device_scale_factor=1,
        )

    page = context.new_page()
    if record_mode == "screen":
        page.bring_to_front()

    recorder = None
    frame_recorder = None
    pre_nav_step = None
    output_path = None

    try:
        if record_mode == "screen":
            # Ensure window metrics are available for cropping
            page.goto("about:blank")
            crop_filter = _compute_crop_filter(page)
            if crop_filter:
                logger.info(f"  Crop filter: {crop_filter}")
            else:
                logger.info("  Crop filter: none (full screen)")
            output_path = output_dir / f"{scene_id}.mp4"
            recorder = _start_screen_recording(output_path, fps, crf, screen_index, crop_filter)

        elif record_mode == "frames":
            output_path = output_dir / f"{scene_id}.mov"
            frame_recorder = FrameCaptureRecorder(output_path, fps, VIEWPORT)
            # Navigate to first page before starting capture (ensures page is ready)
            if scene["steps"] and scene["steps"][0].get("action") == "navigate":
                pre_nav_step = scene["steps"][0]
                url = pre_nav_step["url"]
                full_url = f"{BASE_URL}{url}" if url.startswith("/") else url
                page.goto(full_url)
                if wait_for := pre_nav_step.get("wait_for"):
                    page.wait_for_selector(wait_for, timeout=15000)
            frame_recorder.start(page)

        # Inject click indicator at start
        inject_click_indicator(page)

        if audio_duration:
            logger.info(f"  Audio duration: {audio_duration:.1f}s")
        else:
            logger.info("  No audio for this scene")

        for step in scene["steps"]:
            # Skip the first navigate if we already did it for frames mode
            if record_mode == "frames" and pre_nav_step is not None and step is pre_nav_step:
                continue
            execute_step(page, step, audio_duration, frame_recorder)

        # Small buffer at end
        if frame_recorder:
            frame_recorder.capture_for_duration(500)
        else:
            page.wait_for_timeout(500)

    finally:
        if record_mode == "screen":
            _stop_screen_recording(recorder)
        elif record_mode == "frames" and frame_recorder:
            frame_recorder.stop()
        context.close()

    if record_mode == "playwright":
        # Get the video path (Playwright saves with random name)
        video_path = page.video.path()

        if video_path:
            video_path = Path(video_path)
            final_path = output_dir / f"{scene_id}.webm"
            video_path.rename(final_path)
            logger.info(f"  Saved: {final_path}")
            return final_path

        logger.error(f"  No video recorded for scene: {scene_id}")
        return None

    if record_mode in ("screen", "frames"):
        if output_path and output_path.exists():
            size_mb = output_path.stat().st_size / (1024 * 1024)
            logger.info(f"  Saved: {output_path} ({size_mb:.1f}MB)")
            return output_path

        logger.error(f"  No video recorded for scene: {scene_id}")
        return None

    return None


def record_scenario(
    scenario_name: str,
    scene_filter: str | None = None,
    headless: bool = False,
    record_mode: str = "frames",
    screen_index: str | None = None,
    fps: int = 30,
    crf: int = 12,
) -> list[Path]:
    """Record all scenes with audio-driven timing.

    Outputs raw ProRes 422 HQ files (.mov) - source of truth.
    Compression for distribution is a separate pipeline step.

    Args:
        scenario_name: Name of the scenario to record
        scene_filter: Optional scene ID to record only that scene
        headless: Run browser in headless mode (for CI/servers)
        record_mode: frames (headless ProRes), screen (macOS capture), playwright (low quality), none
        crf: Only used for screen mode (H.264)
    """
    scenario = load_scenario(scenario_name)
    audio_durations = load_audio_durations(scenario_name)

    output_dir = Path("videos") / scenario_name
    output_dir.mkdir(parents=True, exist_ok=True)
    if record_mode == "playwright":
        (output_dir / "raw").mkdir(exist_ok=True)

    scenes = scenario["scenes"]
    if scene_filter:
        scenes = [s for s in scenes if s["id"] == scene_filter]
        if not scenes:
            raise ValueError(f"Scene not found: {scene_filter}")

    logger.info(f"Recording {len(scenes)} scene(s) for scenario: {scenario_name}")
    logger.info(f"Mode: {record_mode} | headless: {headless} | fps: {fps}")

    if record_mode == "screen" and headless:
        raise ValueError("Screen recording requires headful mode. Use --record-mode frames for headless high-quality capture.")

    launch_args = []
    if record_mode == "screen":
        launch_args = [
            "--window-size=1920,1080",
            "--window-position=0,0",
            "--app=http://localhost:30080/",
        ]

    video_paths: list[Path] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, args=launch_args)

        for scene in scenes:
            if record_mode == "none":
                logger.info(f"Driving scene without recording: {scene['id']}")
                context = browser.new_context(viewport=VIEWPORT, device_scale_factor=1)
                page = context.new_page()
                page.bring_to_front()
                inject_click_indicator(page)
                audio_dur = audio_durations.get(scene["id"])
                for step in scene["steps"]:
                    execute_step(page, step, audio_dur)
                page.wait_for_timeout(500)
                context.close()
                continue

            audio_dur = audio_durations.get(scene["id"])
            path = record_scene(browser, scene, audio_dur, output_dir, record_mode, screen_index, fps, crf)
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

    if video_paths:
        total_size = sum(p.stat().st_size for p in video_paths) / (1024 * 1024)
        logger.info(f"Total size: {total_size:.1f}MB")

    return video_paths


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

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
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode (for CI/servers)")
    parser.add_argument(
        "--record-mode",
        choices=["screen", "frames", "playwright", "none"],
        default="frames",
        help="Recording mode: frames (headless ProRes), screen (macOS window capture), playwright (low quality), none (default: frames)",
    )
    parser.add_argument("--screen", help="AVFoundation screen device index (for --record-mode screen)")
    parser.add_argument("--crf", type=int, default=12, help="H.264 CRF for --record-mode screen (lower=better, default: 12)")
    parser.add_argument("--fps", type=int, default=30, help="Frames per second (default: 30)")
    parser.add_argument("--list", action="store_true", help="List available scenarios")
    args = parser.parse_args()

    if args.list:
        list_scenarios()
        return

    record_scenario(
        args.scenario,
        scene_filter=args.scene,
        headless=args.headless,
        record_mode=args.record_mode,
        screen_index=args.screen,
        fps=args.fps,
        crf=args.crf,
    )


if __name__ == "__main__":
    main()
