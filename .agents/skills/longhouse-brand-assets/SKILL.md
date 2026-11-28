---
name: longhouse-brand-assets
description: Work on Longhouse logos, icons, and derivative assets across menu bar, panel, web, and packaging surfaces without drifting geometry or padding.
---

# Longhouse brand assets

Use this when touching logo or icon files, render scripts, or per-surface derivatives.

## Rule: source assets are geometry contracts

- A raw source icon has zero padding, unless the file is explicitly a padded surface asset.
- Each derivative adds its own inset for its own surface. Do not bake menu bar, panel, favicon or app-icon padding into the upstream source.
- If an icon looks smaller than its neighbors, suspect transparent margins first.

## Paths

| What | Path |
|---|---|
| Master logo art | `web/branding/longhouse-logo-master.svg` |
| Menu bar status item (monochrome, edge to edge) | `desktop/LonghouseMenuBarHarness/Sources/LonghouseMenuBarCore/Resources/LonghouseMenuIcon.png` |
| Panel severity icons | same folder: `LonghousePanelIconGreen.png`, `...Yellow.png`, `...Red.png`, `...Gray.png` |
| Render scripts | `web/scripts/render-menubar-icon.mjs`, `web/scripts/generate-icons.sh` |

## Two different menu bar surfaces

- **Status item** in the menu bar: the dedicated monochrome asset, edge to edge, no baked margin. Attention is handled separately from panel branding.
- **Opened panel header**: severity-toned logo derivatives, with an inset where the larger circular badge needs optical balance.

"Make the icon green, yellow or red" is ambiguous between these. Ask which one, because they are different assets and different product decisions.

## Rendering

- Keep the master detailed. Do not flatten tonal variants into one fill unless the surface wants a stencil.
- Pass any padding explicitly to the render script.
- An SVG `viewBox` does not say whether the exported PNG is visually tight. Trim rendered alpha first, so the derivative starts from real painted bounds.

```bash
node web/scripts/render-menubar-icon.mjs <master.svg> <output.png> <width> <height> [tone] [paddingRatio]

# zero-padding, source-style
node web/scripts/render-menubar-icon.mjs web/branding/longhouse-logo-master.svg /tmp/agents/icon.png 36 36 menu 0
# panel output with an explicit inset
node web/scripts/render-menubar-icon.mjs web/branding/longhouse-logo-master.svg /tmp/agents/panel-green.png 72 72 green 0.08
```

## Check

1. Regenerate only the assets you changed.
2. Look at the PNGs, not just the code or SVG. A transparent PNG over an arbitrary viewer background misleads, so composite it onto a dark background yourself. Washed-out or blobby output may be tonal remapping or viewer compositing, so find out which before changing the art.
3. Measure alpha bounds:

```bash
magick desktop/LonghouseMenuBarHarness/Sources/LonghouseMenuBarCore/Resources/LonghouseMenuIcon.png -trim -format 'trimmed=%wx%h%O\n' info:
python3 -c "from PIL import Image; i=Image.open('desktop/LonghouseMenuBarHarness/Sources/LonghouseMenuBarCore/Resources/LonghouseMenuIcon.png').convert('RGBA'); print(i.size, i.getchannel('A').getbbox())"
```

4. Run the harness for the affected surface (dispatched to the wisp runner, so commit and push first): `make menubar-harness MODE=test`.
5. For the installed macOS app, refresh it: `make dogfood-refresh HERE=1` (this working tree, before it lands). The refresh restarts the app.

## Guardrails

- Keep a regression test on the status item source icon. The current one asserts `LonghouseMenuIcon.png` reaches every edge of the bitmap.
- Do not overwrite `LonghouseMenuIcon.png` from the generic derivative generator unless that is the explicit task.
