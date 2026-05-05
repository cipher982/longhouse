---
name: zerg-ui
description: Capture Zerg UI screenshots and debug bundles. Use for UI debugging, QA, and visual verification.
---

# Zerg UI Capture

## Quick Look (Public Pages)
For landing page or public pages, use browser-hub MCP directly:
```python
mcp__browser-hub__browser(action="navigate", url="https://longhouse.ai")
mcp__browser-hub__browser(action="look")  # Screenshot + A11y tree
```

## Local Debug Bundle (Authenticated Views)
Requires `make dev` running. Produces a full debug bundle:

```bash
make ui-capture                           # Timeline + demo data
make ui-capture PAGE=machines             # Machines page
make ui-capture PAGE=health               # Runtime health page
make ui-capture SCENE=empty               # Empty state
make ui-capture SCENE=onboarding-modal    # With modal visible
make ui-capture SCENE=timeline-card-stress VIEWPORT=mobile  # Mobile card layout fixture
make ui-capture PAGE=session-detail SCENE=session-detail-stress  # Session workspace fixture
make ui-capture ALL=1                     # All pages
make qa-ui-workbench                      # Timeline + session fixture set, desktop and mobile
```

**Output:** `artifacts/ui-capture/<timestamp>/`
- `<page>.png` - Screenshot
- `<page>-a11y.json|yml` - Accessibility snapshot (JSON if available, YAML via ariaSnapshot fallback)
- `trace.zip` - Playwright trace (open with `bunx playwright show-trace`)
- `console.log` - Console output
- `manifest.json` - Metadata + paths
- Workbench runs also write `index.html` at the run root for one-page screenshot review

## Reading Bundle Artifacts
```python
# Read manifest to understand what was captured
Read(file_path="artifacts/ui-capture/<timestamp>/manifest.json")

# View screenshot
Read(file_path="artifacts/ui-capture/<timestamp>/timeline.png")

# Check accessibility snapshot for structure (one of these will exist)
Read(file_path="artifacts/ui-capture/<timestamp>/timeline-a11y.json")
Read(file_path="artifacts/ui-capture/<timestamp>/timeline-a11y.yml")
```

## Scenes (Deterministic States)

| Scene | What it sets up |
|-------|-----------------|
| `demo` | Seeds 2 demo sessions (default) |
| `empty` | No data, empty state UI — calls the dev-only session reset endpoint (requires AUTH_DISABLED=1) |
| `onboarding-modal` | Shows first-time setup modal |
| `missing-api-key` | API key required modal visible |
| `timeline-card-stress` | Fixture-backed timeline API responses for card layout QA without relying on live demo DB shape |
| `session-detail-stress` | Fixture-backed managed session workspace with branch seam, tool rows, active runtime strip, and dock controls |

## Visual Regression (CI)
```bash
make qa-ui-baseline           # Run visual baseline tests
make qa-ui-baseline-update    # Update baselines
make qa-ui-baseline-mobile    # Run mobile visual baseline tests
SKIP_LLM=1 make qa-visual-compare  # Desktop app pixel diff against baselines without LLM triage
```

## iOS Layout QA (No Simulator)
The simulator requires auth and can't be scripted past a login screen. For iOS layout work, mock the chrome in HTML and screenshot with Playwright instead:

```bash
# Write an HTML file mimicking the iOS view at iPhone 16 Pro dimensions
cat > /tmp/ios-mock.html << 'EOF'
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=393, initial-scale=1">
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, sans-serif; }
  body { background: #000; color: #fff; width: 393px; height: 852px; }
  /* mirror SwiftUI .bar material: */ .bar { background: rgba(28,28,30,0.92); border-top: 1px solid rgba(255,255,255,0.1); }
</style>
</head>
<body>
  <!-- your mock layout here -->
</body>
</html>
EOF

bunx playwright screenshot --browser chromium --viewport-size "393,852" "file:///tmp/ios-mock.html" /tmp/ios-mock.png
```

Then `Read(/tmp/ios-mock.png)` to inspect with vision. Iterate HTML until the layout is right, then translate to SwiftUI. Not pixel-perfect (no SF Symbols, no blur material) but catches layout problems — crowded rows, wrong spacing, accidental tap targets — in ~2s per iteration.

Add `#Preview` blocks in a `*Previews.swift` file (see `SessionViewPreviews.swift`) for a Xcode canvas view once the structure is settled.

## Gotchas
- Dev must be running: `curl localhost:47300/health`
- Local dev has AUTH_DISABLED=1 (auto-logged-in)
- Animations disabled via CSS injection
- Trace files: `bunx playwright show-trace trace.zip` to debug
- Output is in `artifacts/ui-capture/` (gitignored)
