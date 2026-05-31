"""Build the shipping iPhone hero asset end-to-end.

Zero-to-one pipeline. All inputs regenerate from source-of-truth.
Tomorrow the widget changes — run this script, done.

Pipeline:
  1. Render widget from SwiftUI source (scripts/widget-snapshot)
  2. Generate solo iPhone shot via Gemini 3 Pro Image (OpenRouter)
     - Passes widget PNG as reference image
     - Portrait aspect, high resolution
  3. If text is garbled, second-pass text-fix via Nano Banana Pro (Replicate)
     - Rewrites widget text to match exact strings
  4. Crop + convert to webp at ship resolution
  5. Write to web/public/images/landing/device-iphone.webp

Env vars required:
  OPENROUTER_API_KEY (via ~/git/me/scripts/infisical-get.py)
  REPLICATE_API_TOKEN (for text-fix fallback)
"""
import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
WIDGET_SNAPSHOT_DIR = REPO / "scripts/widget-snapshot"
SHIP_PATH = REPO / "web/public/images/landing/device-iphone.webp"
WORK_DIR = Path("/tmp/iphone-pipeline")
WORK_DIR.mkdir(exist_ok=True)

# Source of truth for widget session text (mirrors widget-snapshot/Sources/main.swift)
WIDGET_TEXT = {
    "header_left": "Longhouse",
    "header_right": "2 waiting",
    "rows": [
        {
            "title": "Fixing auth flow in login",
            "project": "longhouse",
            "attention": "Waiting on you",
            "color": "orange",
        },
        {
            "title": "Deploy pipeline stuck",
            "project": "zerg",
            "attention": "Needs permission",
            "color": "red",
        },
    ],
}


def sh(*args, cwd=None):
    subprocess.run(list(args), check=True, cwd=cwd)


def data_url(path: Path, mime: str = "image/png") -> str:
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()


def get_secret(name: str) -> str:
    result = subprocess.run(
        ["python3", os.path.expanduser("~/git/me/scripts/infisical-get.py"), name],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"infisical-get {name}: {result.stderr}")
    return result.stdout.strip()


# ─── Step 1: Snapshot the widget from Swift source ───────────────────────────
def snapshot_widget() -> Path:
    print("[1/4] snapshot widget from SwiftUI source...")
    sh("swift", "run", "WidgetSnapshot", str(WORK_DIR), cwd=WIDGET_SNAPSHOT_DIR)
    widget = WORK_DIR / "widget-medium.png"
    assert widget.exists(), f"widget snapshot missing: {widget}"
    return widget


# ─── Step 2: Generate solo iPhone via Gemini 3 Pro Image ─────────────────────
SOLO_PHONE_PROMPT = """A single iPhone 15 Pro photographed for a premium tech magazine.

TOUCHSCREEN GLASS FACING THE VIEWER, slight 3/4 perspective, tall portrait composition.

On the phone's black OLED screen, DISPLAY THE ATTACHED WIDGET IMAGE EXACTLY AS-IS in the upper portion of the screen, just below the Dynamic Island. The widget appears as the phone's iOS home-screen widget — same rounded corners, same dark background, same exact text, same colors, same icons. Do NOT rewrite, translate, re-interpret, or stylize any text. Every letter must match the reference image pixel for pixel.

Below the widget, keep the rest of the screen dim — blurred dark abstract wallpaper or solid near-black. Only the widget is lit.

Lighting: warm golden rim light along the right edge of the titanium frame. Subtle cool fill from the left.

BACKGROUND: completely solid PURE BLACK (#000000) — absolutely uniform, no gradient, no floor, no ground plane, no shadow, no reflection, no spill light, no desk, no ambient color. The phone floats on a perfectly flat black field. This is non-negotiable — every pixel of background must be pure #000000 so the image will composite on a dark webpage.

Format: portrait, 1024×1536 or similar 2:3 aspect. Crop tight on the phone — minimal surrounding black, no objects.

CRITICAL: the widget on the screen must be SHARP, CLEAR, LEGIBLE. Interfaces sell the product. No blur, no haze, no obscuring.
"""


def gen_solo_iphone(widget: Path, out: Path) -> Path:
    print("[2/4] generate solo iPhone with Gemini 3 Pro Image...")
    api_key = get_secret("OPENROUTER_API_KEY")
    payload = {
        "model": "google/gemini-3-pro-image-preview",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url(widget)}},
                    {"type": "text", "text": SOLO_PHONE_PROMPT},
                ],
            }
        ],
    }
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/cipher982/longhouse",
        },
    )
    print("  calling gemini-3-pro-image-preview...")
    resp = urllib.request.urlopen(req, timeout=180)
    data = json.loads(resp.read())
    msg = data["choices"][0]["message"]

    images = msg.get("images", [])
    if not images and isinstance(msg.get("content"), list):
        images = [p for p in msg["content"] if isinstance(p, dict) and p.get("type") == "image_url"]
    if not images:
        raise RuntimeError(f"no image in response. keys={list(msg.keys())}")

    url = images[0]["image_url"]["url"]
    b64_data = url.split("base64,", 1)[1]
    out.write_bytes(base64.b64decode(b64_data))
    usage = data.get("usage", {})
    cost = usage.get("cost", "?")
    print(f"  saved: {out} (cost ≈ ${cost})")
    return out


# ─── Step 3 (optional): Text-fix pass via Nano Banana Pro ────────────────────
NANO_BANANA_PRO_VERSION = "712e06a8e122fb7c8dae55dcf7ad6a8e717afb7b1c41c889fc8c5132fd42f374"


def text_fix_prompt() -> str:
    rows = WIDGET_TEXT["rows"]
    return (
        "Edit this iPhone product photo to fix the widget text on the phone screen. "
        "Keep the phone, widget background, rounded corners, bullet colors, lighting, "
        "and every other visual detail EXACTLY the same. Only rewrite the widget text "
        "so it is sharp, legible, and matches this exact content:\n\n"
        f"Header row (small, Longhouse house icon on left): "
        f"'{WIDGET_TEXT['header_left']}' left / '{WIDGET_TEXT['header_right']}' right (orange).\n\n"
        f"First row ({rows[0]['color']} bullet):\n"
        f"  Title (bold white): '{rows[0]['title']}'\n"
        f"  Subtitle: '{rows[0]['project']}   {rows[0]['attention']}' (attention in {rows[0]['color']}).\n\n"
        f"Second row ({rows[1]['color']} bullet):\n"
        f"  Title (bold white): '{rows[1]['title']}'\n"
        f"  Subtitle: '{rows[1]['project']}   {rows[1]['attention']}' (attention in {rows[1]['color']}).\n\n"
        "All text in clean modern sans-serif, perfectly spelled, no artifacts or double-letters. "
        "Do not add new UI elements, icons, or text outside the widget. "
        "Do not alter the phone frame, Dynamic Island, wallpaper, or background."
    )


def text_fix(src: Path, out: Path) -> Path:
    print("[3/4] text-fix pass via Nano Banana Pro...")
    token = os.environ.get("REPLICATE_API_TOKEN") or get_secret("REPLICATE_API_TOKEN")
    body = {
        "version": NANO_BANANA_PRO_VERSION,
        "input": {
            "prompt": text_fix_prompt(),
            "image_input": [data_url(src)],
            "resolution": "2K",
            "aspect_ratio": "match_input_image",
            "output_format": "png",
        },
    }
    req = urllib.request.Request(
        "https://api.replicate.com/v1/predictions",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Prefer": "wait=60",
        },
    )
    resp = urllib.request.urlopen(req, timeout=180)
    pred = json.loads(resp.read())
    pred_id = pred["id"]
    print(f"  id={pred_id} status={pred['status']}")
    while pred["status"] in ("starting", "processing"):
        time.sleep(3)
        poll = urllib.request.Request(
            f"https://api.replicate.com/v1/predictions/{pred_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        pred = json.loads(urllib.request.urlopen(poll, timeout=30).read())
        print(f"  status={pred['status']}")
    if pred["status"] != "succeeded":
        print(json.dumps(pred, indent=2))
        sys.exit(1)
    out_url = pred["output"] if isinstance(pred["output"], str) else pred["output"][0]
    with urllib.request.urlopen(out_url, timeout=120) as r:
        out.write_bytes(r.read())
    print(f"  saved: {out}")
    return out


# ─── Step 4: Crop + convert to shipping webp ─────────────────────────────────
def ship(src: Path, dest: Path, width: int = 1024) -> Path:
    print(f"[4/4] convert to webp → {dest}")
    # Gemini output is already tightly cropped. Do NOT -trim — it produces
    # stair-stepping artifacts when the image has subtle gradient edges.
    sh(
        "magick",
        str(src),
        "-resize",
        f"{width}x",
        "-quality",
        "88",
        str(dest),
    )
    size = dest.stat().st_size
    print(f"  {dest} ({size // 1024} KB)")
    return dest


# ─── Main ────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--text-fix",
        action="store_true",
        help="Run Nano Banana Pro text-fix pass (opt-in: the model often reinvents the whole image, use only if Gemini raw text is broken)",
    )
    ap.add_argument("--ship", action="store_true", help=f"Write final webp to {SHIP_PATH}")
    ap.add_argument("--width", type=int, default=1024, help="Shipping width in px")
    args = ap.parse_args()

    widget = snapshot_widget()
    raw = gen_solo_iphone(widget, WORK_DIR / "iphone-raw.png")

    final = raw
    if args.text_fix:
        final = text_fix(raw, WORK_DIR / "iphone-text-fixed.png")

    preview = WORK_DIR / "iphone-ship-preview.webp"
    ship(final, preview, width=args.width)
    print(f"\npreview: {preview}")
    print(f"  open {preview}")

    if args.ship:
        ship(final, SHIP_PATH, width=args.width)
        print(f"\nshipped: {SHIP_PATH}")
    else:
        print(f"\n(not shipped — re-run with --ship to write {SHIP_PATH})")


if __name__ == "__main__":
    main()
