---
name: landing-hero
description: Generate landing page hero images using real product screenshots composited by AI image models. Captures web timeline, iOS widget, and terminal references then feeds them to Gemini Pro Image for composition.
---

# Landing Hero Image Pipeline

Generate premium hero images for the Longhouse landing page using **real product screenshots** composited by AI image models. The pipeline captures fresh references, feeds them to a multimodal image model, and produces a hero composition.

## The Narrative

The hero tells this story: **"You code in your terminal → Longhouse gives you visibility everywhere."**

Left side: a developer's terminal (Claude Code, Codex TUI, etc.)
Right side: Longhouse surfaces — the web timeline in a browser window, the iOS widget in a phone frame
Connection: a golden arrow/light trail flowing from terminal to Longhouse

Do NOT show "Longhouse web → Longhouse phone." That's not the pitch. The pitch is that Longhouse captures sessions from tools the user already runs.

## Prerequisites

- `make dev` or `make dev-demo` running (for web screenshots)
- OpenRouter API key: `python3 ~/git/me/scripts/infisical-get.py OPENROUTER_API_KEY`
- Xcode 16+ with Swift 6 (for widget snapshot tool — no simulator needed)
- Zeta GCP credentials (for Imagen 4 only, optional — skip if unavailable, Gemini Pro via OpenRouter is the recommended path)

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
/Users/davidrose/.local/bin/claude 'Read server/zerg/routers/auth_browser.py briefly and suggest a fix'

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
- Must use full path `/Users/davidrose/.local/bin/claude` in Terminal.app (PATH not loaded in `-sh`)

## Step 3: Compose Hero via AI Image Model

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

Common issues to check:
- Phone screen facing backward (re-prompt with "screen facing viewer")
- Screenshots redrawn instead of embedded (strengthen "EXACTLY as-is" language)
- Wrong narrative (Longhouse→phone instead of terminal→Longhouse)
- Arrow missing or wrong direction

## Model Comparison (Tested April 2026)

| Model | Input Images | Resolution | UI Text | Device Realism | Best For |
|-------|-------------|------------|---------|----------------|----------|
| **Gemini 3 Pro Image** (OpenRouter) | YES | ~1500x700 | Sharp, legible | Stylized/3D | **Hero composition with real screenshots** |
| **Gemini 3.1 Flash Image** (OpenRouter) | YES | ~1500x700 | Good | Moderate | Fast iteration |
| **Gemini 2.5 Flash Image** (Vertex/Zeta) | YES | ~1470x700 | Decent | Moderate | Free option, lower quality |
| **Imagen 4 Ultra** (Vertex/Zeta) | NO (text-only) | 2816x1536 | Blurry | Photorealistic | Device frames without real screenshots |
| **Imagen 4 Standard** (Vertex/Zeta) | NO (text-only) | 2816x1536 | Blurry | Photorealistic | Same as Ultra, cheaper |

**Verdict:** Gemini Pro Image is the only model that accepts input images AND produces legible UI. Imagen 4 has higher resolution but fabricates all UI content. For production, use Gemini Pro for composition then consider upscaling.

## Vertex AI Direct Access (Imagen 4)

For photorealistic device mockups WITHOUT real screenshot content:

```bash
TOKEN=$(CLOUDSDK_CONFIG=~/.config/gcloud-zeta \
  /opt/homebrew/share/google-cloud-sdk/bin/gcloud auth print-access-token)

curl -s -X POST \
  "https://us-central1-aiplatform.googleapis.com/v1/projects/zeta-phoenix/locations/us-central1/publishers/google/models/imagen-4.0-ultra-generate-001:predict" \
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

- **Automated pipeline:** Single command that captures all three screenshots (web, widget, terminal), feeds to Gemini Pro, inspects with vision, outputs `web/public/images/landing/hero.png`. Could extend `scripts/marketing-screenshots.sh`.
- **Production hero as React component:** CSS device frames (macOS window, iPhone) + animated SVG golden energy + real screenshots. Resolution-independent and responsive. The AI compositions are **design references** — the ship implementation should be code.
- **Phone back workaround:** Gemini renders the iPhone's titanium back ~40% of the time. Consider post-processing or generating multiple variants and selecting the best.
