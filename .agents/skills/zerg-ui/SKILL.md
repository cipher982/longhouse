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
make ui-capture PAGE=chat                 # Chat page
make ui-capture SCENE=empty               # Empty state
make ui-capture SCENE=onboarding-modal    # With modal visible
make ui-capture ALL=1                     # All pages
```

**Output:** `artifacts/ui-capture/<timestamp>/`
- `<page>.png` - Screenshot
- `<page>-a11y.json|yml` - Accessibility snapshot (JSON if available, YAML via ariaSnapshot fallback)
- `trace.zip` - Playwright trace (open with `bunx playwright show-trace`)
- `console.log` - Console output
- `manifest.json` - Metadata + paths

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
| `empty` | No data, empty state UI — calls `DELETE /api/agents/demo` to remove demo sessions (requires AUTH_DISABLED=1) |
| `onboarding-modal` | Shows first-time setup modal |
| `missing-api-key` | API key required modal visible |

## Visual Regression (CI)
```bash
make qa-ui-baseline           # Run visual baseline tests
make qa-ui-baseline-update    # Update baselines
```

## Gotchas
- Dev must be running: `curl localhost:47300/health`
- Local dev has AUTH_DISABLED=1 (auto-logged-in)
- Animations disabled via CSS injection
- Trace files: `bunx playwright show-trace trace.zip` to debug
- Output is in `artifacts/ui-capture/` (gitignored)
