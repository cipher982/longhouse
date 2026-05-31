"""Build the shipping MacBook hero asset.

Renders a MacBook Pro displaying the Longhouse timeline via Gemini 3 Pro Image.
Pure black background so we can alpha-cut it cleanly with rembg.

Output: web/public/images/landing/device-laptop.webp
"""
import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
TIMELINE_REF = REPO / "web/public/images/landing/timeline-preview.png"
SHIP_PATH = REPO / "web/public/images/landing/device-laptop.webp"
WORK_DIR = Path("/tmp/macbook-pipeline")
WORK_DIR.mkdir(exist_ok=True)


def sh(*args, cwd=None):
    subprocess.run(list(args), check=True, cwd=cwd)


def data_url(path: Path, mime: str = "image/png") -> str:
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()


def get_secret(name: str) -> str:
    if os.environ.get(name):
        return os.environ[name]
    result = subprocess.run(
        ["python3", os.path.expanduser("~/git/me/scripts/infisical-get.py"), name],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"infisical-get {name}: {result.stderr}")
    return result.stdout.strip()


MACBOOK_PROMPT = """A single 16-inch MacBook Pro photographed for a premium tech magazine.

OPEN, SCREEN FACING THE VIEWER, slight 3/4 perspective from slightly above, landscape composition. Aluminum silver/space-gray chassis. Visible MacBook keyboard at the bottom, aluminum hinge, subtle bezel around the screen.

On the MacBook screen, DISPLAY THE ATTACHED TIMELINE UI IMAGE EXACTLY AS-IS, filling the entire display area. This is a web browser showing a dark timeline dashboard with session cards. Do NOT rewrite, translate, or stylize any text. Every letter must match the reference image pixel for pixel. The screen is bright and sharp — this is the product.

Lighting: warm golden rim light along the right edge. Subtle cool fill from the left. Screen glow naturally illuminating the keyboard area.

BACKGROUND: completely solid PURE BLACK (#000000) — absolutely uniform, no gradient, no floor, no shadow, no reflection, no spill light, no desk. The MacBook floats on a perfectly flat black field. Non-negotiable.

Format: landscape, 16:10 or 3:2 aspect. Crop tight on the MacBook — minimal surrounding black.

CRITICAL: the timeline UI on the screen must be SHARP, CLEAR, LEGIBLE. Every session card, title, and label must be readable. Interfaces sell the product.
"""


def gen_macbook(timeline_ref: Path, out: Path) -> Path:
    print("[1/3] generate MacBook with Gemini 3 Pro Image...")
    api_key = get_secret("OPENROUTER_API_KEY")
    payload = {
        "model": "google/gemini-3-pro-image-preview",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url(timeline_ref)}},
                    {"type": "text", "text": MACBOOK_PROMPT},
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
    cost = data.get("usage", {}).get("cost", "?")
    print(f"  saved: {out} (cost ≈ ${cost})")
    return out


def alpha_cut(src: Path, out: Path) -> Path:
    print("[2/3] alpha cutout with rembg...")
    sh("rembg", "i", str(src), str(out))
    return out


def ship(src: Path, dest: Path, width: int = 1400) -> Path:
    print(f"[3/3] convert to webp → {dest}")
    sh("magick", str(src), "-resize", f"{width}x", "-quality", "90", str(dest))
    size = dest.stat().st_size
    print(f"  {dest} ({size // 1024} KB)")
    return dest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ship", action="store_true", help=f"Write final webp to {SHIP_PATH}")
    ap.add_argument("--width", type=int, default=1400, help="Shipping width in px")
    args = ap.parse_args()

    raw = gen_macbook(TIMELINE_REF, WORK_DIR / "macbook-raw.png")
    alpha = alpha_cut(raw, WORK_DIR / "macbook-alpha.png")
    preview = WORK_DIR / "macbook-ship-preview.webp"
    ship(alpha, preview, width=args.width)
    print(f"\npreview: {preview}")
    if args.ship:
        ship(alpha, SHIP_PATH, width=args.width)
        print(f"\nshipped: {SHIP_PATH}")
    else:
        print(f"\n(not shipped — re-run with --ship to write {SHIP_PATH})")


if __name__ == "__main__":
    main()
