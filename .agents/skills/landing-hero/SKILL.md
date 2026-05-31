---
name: landing-hero
description: Generate and ship the Longhouse landing hero using separate AI-rendered device assets plus CSS composition. Capture real product references, render each device, crop tightly, then assemble responsively in the hero.
---

# Landing Hero Pipeline

Use this skill when the user says things like:

- "update the hero image"
- "refresh the landing hero"
- "the devices look wrong"
- "redo the landing art"

This skill is the whole workflow. Do not invent a separate process doc or task note.

The current shipping approach is **not** one giant baked hero image. The final product is:

1. three separate device assets
2. tightly cropped
3. assembled in CSS in the landing hero

The AI output is raw material. The hero is the composed stage in code.

## Entry

Assume the user wants the shipping hero updated unless they explicitly ask for concepts only.

Start here:

1. Inspect the current hero in `web/src/components/landing/HeroSection.tsx` and `web/src/styles/landing.css`
2. Inspect the current device assets in `web/public/images/landing/`
3. Bring up local dev and capture desktop + mobile screenshots
4. Decide whether the problem is:
   - asset quality
   - crop/geometry
   - CSS composition
   - or some mix of the three

## Exit

You are done when all of these are true:

- the hero looks intentional on desktop
- the hero still reads on mobile
- the shipped device assets are the only image inputs
- no stale legacy hero image is still referenced or lingering
- the skill/docs do not contradict the implementation
- **you actually looked at the live result** — see "Vision Check Before Calling Done" below

## Vision Check Before Calling Done

**Hard rule: after any hero iteration deploys, fetch the live screenshot and view it with vision tools before telling the user "done."**

Not the local dev server. Not the raw asset. The composed live page, through Cloudflare, rendered by Playwright or curl-into-headless-browser, opened with Read so you (and the model looking at the result) can *see* the pixels.

Why this rule exists:

- Image work routinely breaks silently. Alpha channels, blend modes, `?v=` cache busts, CDN cache layers, wrong file names, lying alt text — none of this shows up in CSS or HTML review.
- Multiple agents have reported "done" on hero work that was visibly broken: semi-transparent phones, black rectangles, assets not actually updated, filenames that don't match the pixels.
- Trusting filenames is not the same as seeing the image. `device-monitor.webp` was actually a MacBook for months and nobody noticed because nobody looked.

Minimum vision check:

1. Fetch the live hero image the way a browser would (`curl https://longhouse.ai/...`) into a fresh filename with timestamp (stale Read-cache is real — see "Read-cache invalidation" below).
2. Take a live Playwright screenshot of `https://longhouse.ai/` at desktop and mobile viewports.
3. Open both with the Read tool and look at them. Confirm: is the asset actually what you think it is? Does it render solid, with clean edges, no transparency issues, no ghosting? Do both viewports hold together?
4. Only *then* call the task done.

If the result does not match what you claimed you shipped, say so out loud before handing back.

## Canonical Outputs

Ship and maintain these files:

- `web/public/images/landing/device-laptop.webp` — MacBook render displaying the timeline
- `web/public/images/landing/device-iphone.webp` — iPhone render displaying the widget
- `web/src/components/landing/HeroSection.tsx`
- `web/src/styles/landing.css`

Naming rule: the file name must match what is actually in the pixels. If the render is a MacBook, the file is `device-laptop.webp`, not `device-monitor.webp`. Agents read file names before they read images; a lying name propagates for months.

## Default Workflow

If the user says "update the hero image", do this exact loop:

1. Inspect current state
   - Read `HeroSection.tsx` and `landing.css`
   - Capture local desktop + mobile screenshots
   - Identify whether the problem is in the assets or the composition

2. Refresh real references if needed
   - timeline/browser reference
   - terminal reference
   - iOS widget/phone reference

3. Generate or revise **separate device renders**
   - one monitor
   - one MacBook
   - one iPhone
   - do not stop at a giant flattened composite unless the user explicitly wants concept art only

4. Crop and compress the assets for shipping
   - remove dead black margins
   - remove hardware elements that should not dominate the stage
   - if the monitor stand is ugly, crop it out at the asset level instead of hiding it in CSS
   - resize/compress to the smallest format that still holds up visually in the composed hero
   - prefer modern formats like WebP for shipped device assets unless there is a clear reason not to

5. Compose in CSS
   - monitor is the dominant element
   - MacBook overlaps lower-left
   - iPhone overlaps lower-right
   - desktop and mobile must both be intentional

6. Verify locally
   - capture a desktop screenshot
   - capture a mobile screenshot
   - adjust until both hold together

7. Clean up
   - remove stale legacy hero assets that are no longer used
   - update this skill if the process changed materially

## Non-Goals

- Do not ship one big promo render just because it looks good in isolation.
- Do not keep empty-margin assets and try to compensate with increasingly weird CSS.
- Do not create a script unless the workflow is truly deterministic and worth automating.
- Do not leave the skill describing an old process after the implementation changes.

## Current Canonical Output

These are the production-facing hero assets:

- `web/public/images/landing/device-laptop.webp`
- `web/public/images/landing/device-iphone.webp`

(`device-macbook.webp` is an older asset from the trio exploration and is currently unreferenced. Delete it if you do another hero pass and still don't need it.)

These are assembled here:

- `web/src/components/landing/HeroSection.tsx`
- `web/src/styles/landing.css`

As of April 17, 2026, the shipping hero uses:

- monitor as the dominant center/right anchor
- MacBook overlapping lower-left
- iPhone overlapping lower-right
- CSS positioning, not a flattened composite image

## The Narrative

The narrative is an unresolved product question, not a locked-in answer. Do not assert one in this skill. The current shipping hero is a trio (monitor + MacBook + iPhone) with copy that does the storytelling. Candidate directions explored April 2026:

- **Timeline-primary** — big timeline, phone secondary. Highest product legibility, weakest differentiation.
- **Terminal → Phone** — MacBook left, big phone right, arc between. Strongest differentiation, hides the timeline.
- **Terminal → Timeline → Phone** — three beats in one frame. Clearest full story, risks everything reading small.
- **Scattered → Consolidated** — grayscale "before" pile of forgotten sessions, bright "after" stage. Infomercial energy; the before must be a *pile* (matches timeline = many cards), not one sad session (that maps to a single terminal, which is wrong).
- **Max overlap** — two devices only (timeline + phone), phone physically covering monitor's right edge. High impact, lets copy carry the story.

Constraints that still hold regardless of direction:

- The iPhone widget is a differentiator and must be visible at meaningful size.
- The Longhouse timeline is the surface users live in. Don't hide it entirely.
- Do not flip the pitch to "Longhouse web → Longhouse phone"; the source is sessions from CLIs the user already runs.

If you are picking a direction, workshop before editing the repo — see `## Exploration Loop`.

## Exploration Loop

If the task is "what should the hero look like" (composition, hierarchy, narrative) — not "fix an asset or CSS" — do NOT open `HeroSection.tsx` first. The shipping hero is a committed opinion; iterating on it directly means fighting the old layout instead of exploring a new one.

Use the workshop harness under this skill: `.agents/skills/landing-hero/workshop/`.

- `variants.html` — isolated `<section id="v*">` hero candidates; each uses the real landing design tokens (`--color-brand-primary: #C9A66B`, `--color-text-primary: #F3EAD9`, `--color-surface-page: #120B09`, `--font-display: "Iowan Old Style", Palatino, Georgia, serif`). System fonts only — adding Google Fonts makes Playwright clip-screenshots non-deterministic.
- `render.py` — Playwright clip-screenshot runner.
- `shot-*.png` — finalists from the April 17, 2026 exploration pass. Variant IDs in `variants.html` (e.g. `vg`) do not always match finalist letters; trust the top-left badge label in each shot over the filename.

Finalists left tabled (user couldn't decide):

- `shot-A-timeline-primary.png` — big timeline, phone absent. Best legibility, weakest differentiation.
- `shot-F-big-phone.png` — MacBook + big phone + arc. Best differentiation, timeline hidden.
- `shot-G-terminal-timeline-phone.png` — three beats in one row. Clearest story, risks reading small.
- `shot-J-scattered-consolidated.png` — grayscale "before" pile → bright "after". Infomercial framing.
- `shot-K-max-overlap.png` — only timeline + phone, phone covers monitor's right edge. High impact.

Constraints that must survive whichever direction is picked:

- iPhone widget visible at meaningful size (it's the differentiator).
- Timeline not hidden entirely (it's the actual product surface users live in).
- Pitch stays "sessions from CLIs you already run → Longhouse" — not "Longhouse web → Longhouse phone."

### Running the harness

```bash
cd .agents/skills/landing-hero/workshop
uv run --with "playwright==1.50.0" python render.py
open shot-*.png
```

To add a new variant, copy an existing `<section id="v*">` in `variants.html`, give it a new id, edit composition, set `VARIANTS = ["vnew"]` in `render.py`, and re-run. Always view the rendered PNG with the Read tool before reporting back.

### Gotchas

- Viewport height in `render.py` must exceed the section's `min-height` or `page.screenshot(clip=...)` will timeout. 1440×1500 @ 2x is safe up to ~780px sections.
- The runner resets `window.scrollTo(0, 0)` before each clip; don't remove that — scroll drift desyncs filenames from content.
- Default `playwright` expects a chromium version that may not be cached. Pin `playwright==1.50.0` and point `EXEC` in `render.py` at a `chrome-mac-arm64/Google Chrome for Testing` under `~/Library/Caches/ms-playwright/chromium-*/`. If the pinned path is missing, run `uv run --with "playwright==1.50.0" playwright install chromium` and update `EXEC`.
- Device assets resolve via `../../../../web/public/images/landing/` from the workshop folder.

### Translating a pick into shipping code

When the user decides, do NOT copy `variants.html` into the app. The workshop uses raw CSS; the landing uses the repo's component + token system. Treat the chosen variant as a reference and rebuild it in:

- `web/src/components/landing/HeroSection.tsx`
- `web/src/styles/landing.css`

Then `make dev`, capture desktop + mobile, and verify both before shipping.

This loop is for layout/story exploration. The AI image pipeline below is for when the device assets themselves need to change.

## Shipping Rules

- Do not ship a single flattened hero image if the layout needs to stay responsive.
- Prefer one image per device, then assemble in code.
- The device assets must be tightly cropped. Empty black margins make CSS tuning lie.
- The shipped device assets must also be compressed aggressively enough that the landing page stays fast.
- If the monitor stand or other unwanted hardware dominates the frame, crop it out at the asset level before trying to mask it in CSS.
- Verify both desktop and mobile with local screenshots before calling it done.

## Operational Notes

- A script is appropriate only for something long, repetitive, and deterministic.
- Most hero updates are not that. They are judgment-heavy asset + CSS work, so the agent should just follow this markdown workflow.
- If you do automate part of it later, the script should support this skill, not replace it.

## Prerequisites

- `make dev` or `make dev-demo` running (for web screenshots)
- OpenRouter API key: `python3 ~/git/me/scripts/infisical-get.py OPENROUTER_API_KEY`
- Xcode 16+ with Swift 6 (for widget snapshot tool — no simulator needed)
- GCP credentials (for Imagen 4 only, optional — skip if unavailable, Gemini Pro via OpenRouter is the recommended path)

## Step 1: Capture Web Screenshots

Use the existing marketing screenshot pipeline:

```bash
# Self-contained: seeds demo DB, starts stack, captures, exits
./scripts/marketing-screenshots.sh

# Or if dev is already running:
uv run scripts/capture_marketing.py
```

Output lands in `web/public/images/landing/`:
- `timeline-preview.png` (1400x900)
- `search-preview.png` (1400x900)
- `session-detail-preview.png` (1400x900)

Manifest: `scripts/screenshots.yaml`

### Gotcha: chromium cache drift

`marketing-screenshots.sh` can fail with `BrowserType.launch: Executable doesn't exist at .../chromium_headless_shell-<rev>/...`. The Playwright pin has rolled forward past the cached browser. Fix once:

```bash
uv run --with playwright playwright install chromium
```

Takes ~2 min. Not a `chromium_headless_shell` vs `chromium` distinction — `playwright install chromium` installs both.

### Adding new screenshots

Add entries to `scripts/screenshots.yaml`:
```yaml
- name: my-new-shot
  url: "/timeline?marketing=true"
  viewport: { width: 1400, height: 900 }
  output: "web/public/images/landing/my-new-shot.png"
```

Frontend readiness contract: pages set `data-screenshot-ready="true"` when loaded.

## Step 2: Capture iOS Widget

A standalone Swift tool renders the widget SwiftUI views to PNG at 3x scale using `ImageRenderer`. Runs as a macOS command-line tool — no simulator needed. Requires Swift 6.0+ (`swift --version` to check). First build takes ~15s.

```bash
cd scripts/widget-snapshot && swift run WidgetSnapshot /tmp
# Or output to a specific directory:
cd scripts/widget-snapshot && swift run WidgetSnapshot artifacts/
```

This produces:
- `widget-small.png` (170x170 @3x = 510x510)
- `widget-medium.png` (364x170 @3x = 1092x510)

The tool source is at `scripts/widget-snapshot/`. To rebuild or modify it:

```swift
// Sources/main.swift
// Mirrors the real widget views from ios/Sources/LonghouseWidget/SessionsWidgetView.swift
// Uses SessionEntry.placeholder data (2 sessions: "Fixing auth flow in login" + "Deploy pipeline stuck")
// Renders both .systemSmall and .systemMedium families
```

If the real widget SwiftUI code changes, update the snapshot tool to match. The mock data comes from `ios/Sources/LonghouseWidget/LonghouseWidget.swift` → `SessionEntry.placeholder`.

### Alternative: Simulator capture

If the app is installed on a booted simulator:

```bash
# Check booted simulators
xcrun simctl list devices available | grep Booted

# Screenshot the full screen
xcrun simctl io <DEVICE_UDID> screenshot /tmp/ios-sim.png
```

This captures the whole screen, not just the widget. The Swift renderer approach is better for isolated widget shots.

## Step 2.5: Capture Terminal Screenshot

Capture a real Claude Code session in iTerm2 (or any standard terminal — avoid Warp, it's non-standard). The key is getting the interactive TUI, not `-p` plain output.

```bash
# 1. Open iTerm2 and launch claude with a task that triggers tool calls
osascript -e 'tell application "iTerm" to activate'
# Then via System Events, type a command like:
~/.local/bin/claude 'Read server/zerg/routers/auth_browser.py briefly and suggest a fix'

# 2. Wait for rendering (8-30s depending on task complexity)
# Capture mid-thought for "thinking" state, or after completion for full output

# 3. Get window ID and screenshot
WID=$(swift -e '
import CoreGraphics
let windows = CGWindowListCopyWindowInfo(.optionOnScreenOnly, kCGNullWindowID) as? [[String: Any]] ?? []
for w in windows {
    let owner = w["kCGWindowOwnerName"] as? String ?? ""
    let wid = w["kCGWindowNumber"] as? Int ?? 0
    if owner.contains("iTerm") { print(wid); break }
}
' 2>/dev/null)
screencapture -l $WID /tmp/claude-terminal.png
```

Tips:
- Capture at ~8s to catch "Prestidigitating. (thinking with medium effort)" state — looks great
- Capture at ~30s for completed output with code analysis, diffs, tool calls
- Use `claude` (no `-p` flag) for the full TUI with status bar, colored prompts
- The `❯` prompt, `zerg git:(main)` status bar, and bullet formatting are the real Claude Code look
- Must use the full path `~/.local/bin/claude` in Terminal.app (PATH not loaded in `-sh`)

## Solo-Device 0→1 Pipeline (recommended default)

For a single shipping device asset (e.g. `device-iphone.webp`), the 0→1 path is:

```bash
cd .agents/skills/landing-hero/workshop
python3 build_iphone_asset.py              # preview only
python3 build_iphone_asset.py --ship       # write device-iphone.webp
```

Pipeline inside the script:

1. **Widget source of truth** — renders `widget-medium.png` via `scripts/widget-snapshot` (Swift tool mirrors `ios/Sources/LonghouseWidget/SessionsWidgetView.swift`). Tomorrow the widget UI changes → re-run, widget re-snapshots from source.
2. **Solo iPhone render** — Gemini 3 Pro Image (OpenRouter) with the widget PNG as the only reference image. Prompt is "single iPhone, TOUCHSCREEN FACING VIEWER, display the attached widget EXACTLY AS-IS, PURE BLACK #000000 BACKGROUND (no gradient, no floor, no spill)". ~$0.14/call, ~15s.
3. **webp encode** — `magick -resize 1024x -quality 88`. No `-trim` (produces stair-stepping on gradient edges). Gemini output is already tightly cropped.

### CSS compositing (critical)

**Always ship a true alpha cutout. Never the matte + `mix-blend-mode: screen` shortcut.**

Gemini renders the iPhone on a pure-black bg (we prompt for that so the model doesn't invent a floor/gradient/desk). The asset must then have its outer black alpha-cut before shipping. Raw `-transparent '#000000'` eats the dark wallpaper too — use corner floodfill with a tight fuzz instead:

```bash
magick iphone-raw.png -alpha set \
  -fill none -fuzz 4% \
  -draw 'alpha 0,0 floodfill' \
  -draw 'alpha W-1,0 floodfill' \
  -draw 'alpha 0,H-1 floodfill' \
  -draw 'alpha W-1,H-1 floodfill' \
  -quality 90 device-iphone.webp
```

Rules:
- **Ship the alpha-cut webp.** `.landing-hero-device--iphone { filter: drop-shadow(...); }` works because the cutout exposes the hero bg through transparent pixels.
- **Do NOT use `mix-blend-mode: screen` as a "dissolve the black matte" shortcut.** It brightens every non-pure-black pixel on the phone body against the hero bg, which makes the phone itself look washed out / semi-transparent — exactly the "transparent phone" bug this pitfall keeps reintroducing. Two agents hit this before the alpha-cut workflow was written down.
- **Do NOT use global `-transparent '#000000'`.** The phone's wallpaper is mostly near-black too — you'll punch holes through the screen content. Floodfill from the corners instead.
- **Asset intrinsic dimensions** in `<img width height>` must match the generated asset (1024×1526 currently), not the old tiny thumbnail. Wrong aspect causes layout-reservation mismatch on load.

**Key learning (April 18, 2026):** Gemini 3 Pro nails legible widget text on the first call about 90% of the time when given a solo-device prompt + real widget PNG reference. The multi-device composed hero prompt is where text fabrication shows up — too many UI elements competing for token budget.

**Re-roll before text-fix.** On a misfire, re-roll Gemini 2–3 times before trying surgical text edits. For the iPhone: second or third roll usually lands clean widget text. For the monitor asset: Gemini fabricates plausible-but-hallucinated UI text, which is acceptable at thumbnail scale in the composed hero (the timeline-preview.png PNG is the real product surface users click to). Don't waste time re-rolling the monitor for perfect UI fidelity — ship it.

**Output resolution floor:** ship ≥ 1024px wide. CSS scales iPhone to ~42% of hero width (~600px rendered at desktop 1440px). Smaller source assets blur catastrophically at that scale factor — this is what made the old `device-iphone.webp` look hallucinated.

### Text-fix fallback (opt-in, unreliable)

If Gemini's first pass has garbled text, you can try Nano Banana Pro on Replicate with `--text-fix`. **Caveat:** in testing it often reinvents the entire image (e.g. adds a laptop + monitor + desk scene even when told "keep everything exactly the same"). Re-roll Gemini 2-3 times before reaching for text-fix.

### Anti-patterns (burned a day learning these)

1. **Do NOT crop a solo phone out of a wide 3-device composition.** You get ~520×704 (the raw) or ~234×420 (after tight crop) — both blur at hero scale. Generate the phone solo from the start.
2. **Do NOT use general-purpose upscalers (Clarity, Real-ESRGAN) to "fix" hallucinated text.** They sharpen garbage into confidently-wrong garbage. Upscalers can't read.
3. **Do NOT build multi-stage "blank phone → AI paste widget" pipelines.** Multi-image edit models (Qwen, Seedream) route the paste to whichever screen has the biggest canvas, regardless of prompt. Even "iPhone only" directives fail when a monitor is in the scene.
4. **Do NOT run `magick -trim` on Gemini output.** The transparent/gradient edges produce stair-stepping artifacts. Gemini already crops tight.
5. **Do NOT ask Nano Banana Pro to "edit only the text" on a photo.** It treats any edit prompt as a creative license to reinvent the scene. For text-only surgery, the tooling isn't there yet.
6. **Do NOT ship a phone asset with a floor/gradient/vignette background.** Mid-gray pixels create a visible matte against the hero bg no matter how you composite. Force a uniform #000000 bg in the prompt, then alpha-cut the background at build time (corner floodfill, fuzz ≤4%).
7. **Do NOT ship the raw black-matte webp and rely on `mix-blend-mode: screen`.** `screen` brightens every non-pure-black pixel on the phone body, making the phone itself look washed out / semi-transparent — the "transparent phone" bug. Always alpha-cut and composite normally with `drop-shadow`.
8. **Do NOT alpha-cut with global `-transparent '#000000'`.** The phone's wallpaper is near-black; a global threshold punches holes through the screen content. Floodfill from the four corners instead.

## Reference: Multi-Device Composed Hero

Use the references below to create the wide 3-device composed hero (the legacy approach — kept for reference, but prefer the solo-device pipeline above for individual assets). This section is reference material for generating assets. It is not the final shipping pattern by itself.

### Recommended: Gemini 3 Pro Image via OpenRouter

This model accepts multiple input images and composes them with high fidelity. Pass all three reference screenshots (terminal, web timeline, widget) as input images.

```python
import base64, json, urllib.request, subprocess, os

api_key = subprocess.run(
    ["python3", os.path.expanduser("~/git/me/scripts/infisical-get.py"), "OPENROUTER_API_KEY"],
    capture_output=True, text=True
).stdout.strip()

# Load all three real screenshots
with open('/tmp/claude-terminal.png', 'rb') as f:
    terminal_b64 = base64.b64encode(f.read()).decode()
with open('web/public/images/landing/timeline-preview.png', 'rb') as f:
    timeline_b64 = base64.b64encode(f.read()).decode()
with open('/tmp/widget-medium.png', 'rb') as f:
    widget_b64 = base64.b64encode(f.read()).decode()

prompt = PROMPT  # See "The Prompt" section below

payload = {
    "model": "google/gemini-3-pro-image-preview",
    "messages": [{
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{terminal_b64}"}},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{timeline_b64}"}},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{widget_b64}"}},
            {"type": "text", "text": prompt}
        ]
    }]
}

req = urllib.request.Request(
    "https://openrouter.ai/api/v1/chat/completions",
    data=json.dumps(payload).encode(),
    headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/cipher982/longhouse",
    }
)

resp = urllib.request.urlopen(req, timeout=180)
data = json.loads(resp.read())
msg = data["choices"][0]["message"]

# Image in msg["images"] as list of {"image_url": {"url": "data:image/png;base64,..."}}
images = msg.get("images", [])
if not images and isinstance(msg.get("content"), list):
    images = [p for p in msg["content"] if isinstance(p, dict) and p.get("type") == "image_url"]
if images:
    url = images[0]["image_url"]["url"]
    b64_data = url.split("base64,", 1)[1]
    with open('/tmp/hero-output.png', 'wb') as f:
        f.write(base64.b64decode(b64_data))
else:
    print("No image in response. Keys:", list(msg.keys()))
```

### The Prompt (v34 "editorial-wide" — locked-in winner)

The winning style is **editorial product photography** — three devices with generous spacing on an ultra-dark surface, as if shot for a premium tech magazine. The #1 priority is **screenshot legibility** — every screen must be sharp, clear, and readable. The interfaces sell the product, not the lighting.

```
Three real product screenshots attached:
1. A real Claude Code terminal session — dark terminal, colored text
2. The Longhouse web timeline dashboard — dark UI, session cards
3. An iOS home screen widget — small widget with session summaries

Create a wide editorial product photograph. Three Apple devices arranged with
generous spacing on an ultra-dark surface, as if photographed for a premium
tech magazine.

LEFT THIRD: FIRST screenshot inside a MacBook Pro laptop. Screen faces the
viewer with slight perspective. The terminal content is CLEARLY VISIBLE —
every line of code sharp and legible. Cool-toned lighting.

CENTER THIRD: SECOND screenshot inside an Apple Pro Display XDR on its
aluminum stand. Front-facing, largest device. The Longhouse timeline UI is
the star — session cards, dates, labels all crisp and readable. Warm golden
ambient lighting.

RIGHT THIRD: THIRD screenshot inside an iPhone 15 Pro, TOUCHSCREEN GLASS
FACING THE VIEWER. Widget content visible on screen. Warm lighting matching
the monitor.

A delicate golden thread of light arcs between all three devices at desk level.

CRITICAL: Embed ALL THREE screenshots EXACTLY as-is. The UI on EVERY screen
must be SHARP, CLEAR, and LEGIBLE. This is product photography — the
interfaces are what sell. No artistic blur, no atmospheric haze, no obscuring.

Background: nearly black. Lighting is dramatic but controlled — illuminates
screens and device edges only. Premium, clean, editorial.
```

### Why v34 Wins

Through 34 iterations, these are the key learnings:

- **"Editorial product photography"** framing gets Gemini to prioritize screenshot fidelity over mood
- **"Nearly front-facing with slight perspective"** is the sweet spot — enough angle for depth, flat enough for legibility
- **"Generous spacing"** prevents device overlap that obscures screens
- **"The interfaces are what sell"** explicitly tells the model not to sacrifice clarity for atmosphere
- **Cool-toned left / warm-toned right** creates the before→after narrative without heavy-handed grading
- **"Delicate golden thread at desk level"** keeps the connecting element subtle and grounded

### Prompt Engineering Rules (Learned from 34 iterations)

- **"EXACTLY as-is"** for every provided screenshot — without this, the model redraws UI
- **"TOUCHSCREEN GLASS FACING THE VIEWER"** for the phone — it renders the titanium back ~40% of the time regardless, but this helps
- **"SHARP, CLEAR, and LEGIBLE"** must appear in the prompt — the model defaults to atmospheric blur without it
- **Describe the composition as product photography, not art** — "editorial", "tech magazine", "product photograph" produce clearer results than "cinematic", "dramatic", "hero image"
- **Three input images >> two** — passing the real terminal screenshot eliminates the biggest source of fabrication
- **Generous spacing > tight grouping** — overlapping devices cause screen occlusion
- **Front-facing > angled** for screen readability, but slight perspective adds depth
- **Describe Claude Code accurately** — it has a `❯` chevron prompt, colored tool pills, dimmed thinking text, and a status bar. NOT a green-text retro terminal

### Using MCP image-hub instead

The `image-hub` MCP server routes through OpenRouter but `generate_image` only takes a text prompt — it does NOT accept input images. For multi-image composition, call the OpenRouter API directly as shown above. `image-hub` is useful for quick text-only concept exploration.

## Step 4: Inspect and Iterate

Always view generated images with vision before presenting to the user:

```python
Read(file_path="/tmp/hero-output.png")
```

### Read-cache invalidation

If you Read the same `/tmp/output.png` path after regenerating, the model may still see the previous version from cache. Two workarounds:

1. **Fresh filename per attempt** — write to `/tmp/hero-output-$(date +%s).png` so every Read hits a new path.
2. **Resize + rename** — if an image exceeds the 2000px dimension limit for multi-image turns, `magick input.png -resize 1400x /tmp/shot-$(date +%s%N).png` produces a smaller file on a fresh path in one step.

### Playwright scripts must live in-repo

Running an ad-hoc `chromium.launch()` script from `/tmp/foo.mjs` fails — the repo's Playwright install doesn't resolve modules from outside the project root. Drop the mjs file inside the repo root (gitignored or cleaned up after) and import `'playwright'` normally.

Common issues to check:
- Phone screen facing backward (re-prompt with "screen facing viewer")
- Screenshots redrawn instead of embedded (strengthen "EXACTLY as-is" language)
- Wrong narrative (Longhouse→phone instead of terminal→Longhouse)
- Arrow missing or wrong direction
- Giant empty black padding around the device render
- Monitor stand or desk surface still visible when it should be cropped away

## Current Shipping Layout

Current composition code lives in:

- `web/src/components/landing/HeroSection.tsx`
- `web/src/styles/landing.css`

The important design decision is that the hero is a **stage**, not an image:

- text column and device stage sit side-by-side on desktop
- stage collapses below the copy on narrower widths
- device overlap is explicit absolute positioning, not auto-sized grid overlap

If you regenerate assets, keep these invariants:

- monitor stays the largest element
- MacBook remains readable at lower-left
- iPhone stays visible at mobile widths
- the UI should still read in dark mode against the landing background

## Model Comparison (Tested April 2026)

| Model | Input Images | Resolution | UI Text | Device Realism | Best For |
|-------|-------------|------------|---------|----------------|----------|
| **Gemini 3 Pro Image** (OpenRouter) | YES | ~1500x700 | Sharp, legible | Stylized/3D | **Hero composition with real screenshots** |
| **Gemini 3.1 Flash Image** (OpenRouter) | YES | ~1500x700 | Good | Moderate | Fast iteration |
| **Gemini 2.5 Flash Image** (Vertex) | YES | ~1470x700 | Decent | Moderate | Free option, lower quality |
| **Imagen 4 Ultra** (Vertex) | NO (text-only) | 2816x1536 | Blurry | Photorealistic | Device frames without real screenshots |
| **Imagen 4 Standard** (Vertex) | NO (text-only) | 2816x1536 | Blurry | Photorealistic | Same as Ultra, cheaper |

**Verdict:** Gemini Pro Image is the only model that accepts input images AND produces legible UI. Imagen 4 has higher resolution but fabricates all UI content. For production, use Gemini Pro for composition then consider upscaling.

## Vertex AI Direct Access (Imagen 4)

For photorealistic device mockups WITHOUT real screenshot content:

```bash
TOKEN=$(CLOUDSDK_CONFIG=~/.config/gcloud \
  /opt/homebrew/share/google-cloud-sdk/bin/gcloud auth print-access-token)

curl -s -X POST \
  "https://us-central1-aiplatform.googleapis.com/v1/projects/your-gcp-project/locations/us-central1/publishers/google/models/imagen-4.0-ultra-generate-001:predict" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "instances": [{"prompt": "..."}],
    "parameters": {
      "sampleCount": 2,
      "aspectRatio": "16:9",
      "sampleImageSize": "2K",
      "enhancePrompt": true,
      "addWatermark": false,
      "outputOptions": {"mimeType": "image/png"}
    }
  }'
```

Key params:
- `sampleImageSize`: must be string `"1K"` or `"2K"` (NOT integer)
- `enhancePrompt`: true lets the model rewrite your prompt for better results
- `addWatermark`: false disables SynthID (also enables `seed` for determinism)
- `sampleCount`: up to 4 variants per call

Available models: `imagen-4.0-generate-001`, `imagen-4.0-ultra-generate-001`

## iOS Widget Source of Truth

Widget views: `ios/Sources/LonghouseWidget/SessionsWidgetView.swift`
Widget data model: `ios/Sources/LonghouseWidget/LonghouseWidget.swift`
Shared models: `ios/Sources/Shared/SessionModels.swift`
Xcode project: `ios/XcodeHarness/LonghouseIOS.xcodeproj`

The widget supports `.systemSmall` (count only) and `.systemMedium` (count + session list).

SwiftUI previews are built in — open the widget file in Xcode to see them:
```
#Preview("Medium - Sessions", as: .systemMedium)
#Preview("Medium - Empty", as: .systemMedium)
#Preview("Small - Sessions", as: .systemSmall)
#Preview("Small - Empty", as: .systemSmall)
```

## Landing Page Code

Hero section: `web/src/components/landing/HeroSection.tsx`
All landing components: `web/src/components/landing/`
Landing styles: `web/src/styles/landing.css`
Screenshot frame component: `web/src/components/landing/AppScreenshotFrame.tsx`
Product showcase (tabbed screenshots): `web/src/components/landing/ProductShowcase.tsx`

## Future Work

- **Automated pipeline:** Single command that captures all three screenshots, generates three device renders, crops them, and writes `device-monitor.png`, `device-macbook.png`, and `device-iphone.png`.
- **Production hero as React component:** Keep the current CSS-composed approach, or later replace AI hardware shells with code-native device frames plus real screenshots if the assets drift too much.
- **Phone back workaround:** Gemini renders the iPhone's titanium back ~40% of the time. Consider post-processing or generating multiple variants and selecting the best.
