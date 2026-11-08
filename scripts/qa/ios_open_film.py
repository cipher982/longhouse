#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow", "numpy"]
# ///
"""L3 of specs/ios-transcript-stability.md: film a real session opening in the
iOS Simulator and measure how the page fills in.

    scripts/qa/ios_open_film.py --udid <sim udid> --session <longhouse id> [--seconds 14] [--out DIR]

Needs the app already installed signed in (`scripts/ops/sim.sh deploy`). The
script relaunches the app on the session while `simctl io recordVideo` runs,
extracts frames at 12 fps, and reports from the transcript region:

  first_content_s   first frame with real content in the transcript area
  settled_s         last frame whose change from the previous frame exceeds the noise floor
  busy_frames       frames with a visible change after first content (a still page has 0)
  big_jumps         frames where more than 8% of the transcript region changed at once
                    (a scroll or reflow, not a text token arriving)

plus a contact sheet of the frames after first content. It records the screen
the way a reader sees it; it knows nothing about the app's internals, so it
cannot be fooled by the app reporting its own success.

Owns what it starts: the recorder process group is signalled and reaped in
`finally`; the simulator and app are the caller's and are left as found.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
FPS = 12


def run(*args: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=True, text=True, capture_output=True, **kw)


def record(udid: str, session: str, seconds: float, out: Path) -> Path:
    video = out / "open.mp4"
    recorder = subprocess.Popen(
        ["xcrun", "simctl", "io", udid, "recordVideo", "--codec=h264", "--force", str(video)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        time.sleep(1.5)  # let the recorder attach
        env = dict(os.environ, SIM_UDID=udid)
        subprocess.run([str(ROOT / "scripts/ops/sim.sh"), "launch", session], check=True, env=env, capture_output=True)
        time.sleep(seconds)
    finally:
        try:
            os.killpg(recorder.pid, signal.SIGINT)  # SIGINT finalizes the mp4
            recorder.wait(timeout=20)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            os.killpg(recorder.pid, signal.SIGKILL)
            recorder.wait()
    return video


def extract(video: Path, frames: Path) -> list[Path]:
    frames.mkdir(parents=True, exist_ok=True)
    run("ffmpeg", "-loglevel", "error", "-i", str(video), "-vf", f"fps={FPS},scale=390:-1", str(frames / "%04d.png"))
    return sorted(frames.glob("*.png"))


def analyze(files: list[Path]) -> dict:
    arrays = []
    for f in files:
        with Image.open(f) as im:
            arrays.append(np.asarray(im.convert("L"), dtype=np.int16))
    h = arrays[0].shape[0]
    top, bottom = int(h * 0.12), int(h * 0.72)  # transcript region, clear of the nav bar and floating composer
    region = [a[top:bottom] for a in arrays]
    diffs = [0.0] + [float((np.abs(region[i] - region[i - 1]) > 24).mean()) for i in range(1, len(region))]
    # "content": enough dark-on-light structure, i.e. not a blank page or a spinner on flat color
    contrast = [float(r.std()) for r in region]
    first = next((i for i, c in enumerate(contrast) if c > 22), None)
    noise = 0.002
    busy = [i for i in range(first + 1, len(diffs)) if diffs[i] > noise] if first is not None else []
    big = [i for i in busy if diffs[i] > 0.08]
    return {
        "frames": len(files),
        "fps": FPS,
        "first_content_s": None if first is None else round(first / FPS, 2),
        "settled_s": None if not busy else round(busy[-1] / FPS, 2),
        "busy_frames": len(busy),
        "big_jumps": len(big),
        "big_jump_frames": big[:20],
        "diff_series": [round(d, 4) for d in diffs],
    }


def sheet(files: list[Path], start: int, out: Path) -> None:
    span = files[start:]
    picks = [span[round(i * (len(span) - 1) / 11)] for i in range(min(12, len(span)))] if span else []
    if picks:
        subprocess.run(["magick", *map(str, picks), "-resize", "200x", "-bordercolor", "#222", "-border", "3", "+append", str(out)], check=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--udid", required=True)
    ap.add_argument("--session", required=True)
    ap.add_argument("--seconds", type=float, default=14)
    ap.add_argument("--out", default=f"/tmp/agents/ios-open-film/{time.strftime('%Y%m%dT%H%M%S')}")
    ap.add_argument("--label", default="")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    frames_dir = out / "frames"
    try:
        video = record(args.udid, args.session, args.seconds, out)
        files = extract(video, frames_dir)
        if len(files) < 5:
            print("recording produced fewer than 5 frames", file=sys.stderr)
            return 2
        report = analyze(files)
        first = int((report["first_content_s"] or 0) * FPS)
        sheet(files, first, out / "open.sheet.png")
        report["session"] = args.session
        report["label"] = args.label
        (out / "report.json").write_text(json.dumps(report, indent=2))
        print(json.dumps({k: v for k, v in report.items() if k != "diff_series"}))
        return 0
    finally:
        shutil.rmtree(frames_dir, ignore_errors=True)  # the sheet and report are the evidence


if __name__ == "__main__":
    sys.exit(main())
