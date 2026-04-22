---
name: longhouse-brand-assets
description: Work on Longhouse logos, icons, and derivative assets across menu bar, panel, web, and packaging surfaces without drifting geometry or padding.
---

# Longhouse Brand Assets

Use this when touching Longhouse logo/icon files, render scripts, or per-surface derivatives.

## Core Rule

Source assets are geometry contracts.

- The raw source icon should be **zero padding** unless the source file is explicitly a padded surface asset.
- Each derivative adds its own inset for its own surface.
- Do not bake menu bar, panel, favicon, or app-icon padding into the upstream source just because one surface wants breathing room.

If an icon suddenly looks "smaller" than neighboring UI, suspect transparent margins first.

## Current Canonical Paths

- Master logo art: `web/branding/longhouse-logo-master.svg`
- Menu bar status source asset: `desktop/LonghouseMenuBarHarness/Sources/LonghouseMenuBarCore/Resources/LonghouseMenuIcon.png`
- Panel severity assets:
  - `desktop/LonghouseMenuBarHarness/Sources/LonghouseMenuBarCore/Resources/LonghousePanelIconGreen.png`
  - `desktop/LonghouseMenuBarHarness/Sources/LonghouseMenuBarCore/Resources/LonghousePanelIconYellow.png`
  - `desktop/LonghouseMenuBarHarness/Sources/LonghouseMenuBarCore/Resources/LonghousePanelIconRed.png`
  - `desktop/LonghouseMenuBarHarness/Sources/LonghouseMenuBarCore/Resources/LonghousePanelIconGray.png`
- Render pipeline:
  - `web/scripts/render-menubar-icon.mjs`
  - `web/scripts/generate-icons.sh`

## Menu Bar Split

Do not blur these surfaces together:

- **Status item** in the macOS menu bar:
  - keep the dedicated monochrome source asset
  - should be edge-to-edge, no baked moat
  - attention is handled separately from the panel branding treatment
- **Opened panel header**:
  - uses severity-toned Longhouse logo derivatives
  - may intentionally add inset for optical balance in the larger circular badge

If the user says "make the icon green/yellow/red," confirm whether they mean the opened panel or the actual menu bar extra. Those are different assets and different product decisions.

## Rendering Rules

- Keep the master art detailed; do not flatten tonal variants into one solid fill unless the surface explicitly wants a stencil.
- If a derivative needs padding, pass it explicitly through the render script.
- Do not trust the SVG `viewBox` alone to tell you whether the exported PNG is visually tight.
- Trim rendered alpha before final sizing/extending so the derivative starts from real painted bounds.

Current render contract:

```bash
node web/scripts/render-menubar-icon.mjs <master.svg> <output.png> <width> <height> [tone] [paddingRatio]
```

Examples:

```bash
# Zero-padding source-style output
node web/scripts/render-menubar-icon.mjs web/branding/longhouse-logo-master.svg /tmp/icon.png 36 36 menu 0

# Panel output with explicit inset
node web/scripts/render-menubar-icon.mjs web/branding/longhouse-logo-master.svg /tmp/panel-green.png 72 72 green 0.08
```

## QA Loop

1. Regenerate only the assets you actually changed.
2. Inspect the actual PNGs, not just the code or SVG.
3. Measure alpha bounds before calling it done.
4. Run the harness/tests for the affected surface.
5. If this affects the installed macOS app, refresh the real app and restart it.

Useful commands:

```bash
# Show tight painted bounds inside a PNG
magick desktop/LonghouseMenuBarHarness/Sources/LonghouseMenuBarCore/Resources/LonghouseMenuIcon.png -trim -format 'trimmed=%wx%h%O\n' info:

# Double-check alpha bounds with Pillow
python3 - <<'PY'
from PIL import Image
img = Image.open('desktop/LonghouseMenuBarHarness/Sources/LonghouseMenuBarCore/Resources/LonghouseMenuIcon.png').convert('RGBA')
print(img.size, img.getchannel('A').getbbox())
PY

# Menu bar harness verification
make menubar-harness MODE=test

# Refresh the installed app after menu bar/runtime changes
make dogfood-refresh
launchctl kickstart -k gui/$(id -u)/ai.longhouse.app
```

## Guardrails

- Add or keep a regression test when touching the status item source icon. The current test asserts `LonghouseMenuIcon.png` reaches every edge of the bitmap.
- Do not overwrite `LonghouseMenuIcon.png` from the generic derivative generator unless that is the explicit task.
- Do not accept "looks about right" from a transparent PNG over an arbitrary viewer background. Check the literal pixels or composite onto a dark background yourself.
- When a generated PNG looks washed out or blob-like, inspect whether the issue is tonal remapping or viewer compositing before changing the art again.
