# Marketing Screenshot Tooling Overhaul

**Created:** 2026-01-03
**Status:** ✅ Implemented
**Implemented:** 2026-01-03

---

## Implementation Notes

All phases completed. Key deviations from spec:

- Marketing mode toggle is in `main.tsx` (not App.tsx), following existing `?effects=` pattern
- Thread resolution uses `title` param (not `name`) since Thread model uses `title` field
- Capture script is ~130 lines (not 50), but still manifest-driven with no clicking/injection
- Threads endpoint doesn't enforce owner filtering (noted for future multi-user hardening)

**Commands:**
```bash
make marketing-list      # List 6 screenshots
make marketing-validate  # Check outputs exist
make marketing-capture   # Seed + capture all
make marketing-single NAME=chat-preview  # Capture one
```

**URLs:**
- `/canvas?workflow=health&marketing=true`
- `/chat?thread=marketing&marketing=true`
- `/dashboard?marketing=true`

---

## Problem Statement

The current marketing screenshot system is **imperative and fragile**:

```
Current flow:
Seed DB → Launch Playwright → Navigate → Sleep (arbitrary) → Click sidebar
→ Sleep → Inject CSS → Screenshot → Crop in code → Repeat for each...
```

**Pain points:**
- Chat screenshot broken (URL `/chat/14` doesn't exist, redirects to dashboard)
- Arbitrary sleeps instead of waiting for app readiness
- CSS injection is a hack that can't be previewed
- Crop dimensions buried in Python code
- Every step can drift as UI evolves
- Not repeatable — works sometimes, fails others

## Solution: Product Affordances for Automation

Instead of fighting the UI with Playwright clicks and waits, add **four affordances** that make screenshots addressable and deterministic:

1. **URL Addressability** — Every marketing state reachable via URL with names
2. **Ready Signals** — App signals when it's ready for capture
3. **Marketing CSS Toggle** — Built-in styling via URL param, no injection
4. **Screenshot Manifest** — Declarative YAML defines what to capture

**Target flow:**
```
Navigate to URL → Wait for ready signal → Screenshot
```

That's it. 40 lines of Python. No clicking, no injection, no arbitrary sleeps.

---

## Architecture

### URL Scheme

```
/canvas?workflow=health&marketing=true
/canvas?workflow=inbox&marketing=true
/canvas?workflow=home&marketing=true
/chat?thread=marketing&marketing=true
/dashboard?marketing=true
```

- `workflow=<name>` — Resolves workflow by name, loads it
- `thread=<name>` — Resolves thread by name, selects it
- `marketing=true` — Enables marketing CSS mode

### Ready Signals

Each page sets `data-ready="true"` on `<body>` when fully loaded:

```tsx
// Canvas: ready when workflow loaded + nodes rendered
useEffect(() => {
  if (workflowLoaded && nodes.length > 0 && edgesRendered) {
    document.body.setAttribute('data-ready', 'true');
  }
  return () => document.body.removeAttribute('data-ready');
}, [workflowLoaded, nodes, edgesRendered]);
```

```tsx
// Chat: ready when thread loaded + messages rendered
useEffect(() => {
  if (threadLoaded && messages.length > 0) {
    document.body.setAttribute('data-ready', 'true');
  }
  return () => document.body.removeAttribute('data-ready');
}, [threadLoaded, messages]);
```

```tsx
// Dashboard: ready when agents loaded
useEffect(() => {
  if (!isLoading && agents.length > 0) {
    document.body.setAttribute('data-ready', 'true');
  }
  return () => document.body.removeAttribute('data-ready');
}, [isLoading, agents]);
```

### Marketing CSS

File: `apps/zerg/frontend-web/src/styles/marketing-mode.css`

Enabled via URL param, applied as body class:

```tsx
// In App.tsx or layout component
const [searchParams] = useSearchParams();
const isMarketing = searchParams.get('marketing') === 'true';

useEffect(() => {
  if (isMarketing) {
    document.body.classList.add('marketing-mode');
  }
  return () => document.body.classList.remove('marketing-mode');
}, [isMarketing]);
```

Contains the "vivid" styling currently injected by capture script:
- Glowing nodes (purple agents, amber triggers)
- Glowing edges (blue/cyan)
- Hidden UI chrome (minimap, controls, sidebar) for cleaner shots
- Enhanced gradients and shadows

### Screenshot Manifest

File: `scripts/screenshots.yaml`

```yaml
# Marketing screenshot definitions
# All paths relative to apps/zerg/frontend-web/

base_url: http://localhost:30080

screenshots:
  scenario-health:
    description: Health workflow for scenario card
    url: /canvas?workflow=health&marketing=true
    viewport: { width: 1000, height: 700 }
    crop: { x: 100, y: 100, width: 800, height: 500 }
    output: public/images/landing/scenario-health.png

  scenario-inbox:
    description: Email workflow for scenario card
    url: /canvas?workflow=inbox&marketing=true
    viewport: { width: 1000, height: 700 }
    crop: { x: 100, y: 100, width: 800, height: 500 }
    output: public/images/landing/scenario-inbox.png

  scenario-home:
    description: Home automation workflow for scenario card
    url: /canvas?workflow=home&marketing=true
    viewport: { width: 1000, height: 700 }
    crop: { x: 100, y: 100, width: 800, height: 500 }
    output: public/images/landing/scenario-home.png

  canvas-preview:
    description: Full canvas view for nerd section
    url: /canvas?workflow=inbox&marketing=true
    viewport: { width: 1400, height: 900 }
    output: public/images/landing/canvas-preview.png

  chat-preview:
    description: Jarvis chat conversation
    url: /chat?thread=marketing&marketing=true
    viewport: { width: 1400, height: 900 }
    output: public/images/landing/chat-preview.png

  dashboard-preview:
    description: Dashboard with agents
    url: /dashboard?marketing=true
    viewport: { width: 1400, height: 900 }
    output: public/images/landing/dashboard-preview.png
```

### Capture Script

File: `scripts/capture_marketing.py`

```python
#!/usr/bin/env python3
"""
Marketing screenshot capture.

Reads manifest, navigates to URLs, waits for ready signal, screenshots.
No clicking, no CSS injection, no arbitrary waits.

Usage:
    uv run scripts/capture_marketing.py              # Capture all
    uv run scripts/capture_marketing.py --name chat  # Capture one
    uv run scripts/capture_marketing.py --list       # List available
    uv run scripts/capture_marketing.py --validate   # Check outputs exist
"""

import argparse
import sys
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

MANIFEST_PATH = Path(__file__).parent / 'screenshots.yaml'
READY_TIMEOUT = 15000  # 15 seconds

def load_manifest():
    with open(MANIFEST_PATH) as f:
        return yaml.safe_load(f)

def capture_screenshot(browser, name: str, config: dict, base_url: str):
    """Capture a single screenshot."""
    page = browser.new_page(viewport={
        'width': config['viewport']['width'],
        'height': config['viewport']['height']
    })

    url = f"{base_url}{config['url']}"
    print(f"  Navigating to {config['url']}")
    page.goto(url)

    # Wait for app to signal readiness
    try:
        page.wait_for_selector('[data-ready="true"]', timeout=READY_TIMEOUT)
    except PlaywrightTimeout:
        print(f"  ⚠ Warning: Ready signal not received for {name}, capturing anyway")

    # Small buffer for any final renders
    page.wait_for_timeout(500)

    # Build screenshot args
    output_path = Path('apps/zerg/frontend-web') / config['output']
    output_path.parent.mkdir(parents=True, exist_ok=True)

    screenshot_args = {'path': str(output_path)}
    if 'crop' in config:
        screenshot_args['clip'] = config['crop']

    page.screenshot(**screenshot_args)

    size_kb = output_path.stat().st_size / 1024
    print(f"  ✓ {name} ({size_kb:.0f} KB)")

    page.close()

def capture_all(manifest: dict, names: list[str] | None = None):
    """Capture screenshots."""
    base_url = manifest['base_url']
    screenshots = manifest['screenshots']

    if names:
        screenshots = {k: v for k, v in screenshots.items() if k in names}
        if not screenshots:
            print(f"No screenshots found matching: {names}")
            return False

    print(f"\n📸 Capturing {len(screenshots)} screenshots...\n")

    with sync_playwright() as p:
        browser = p.chromium.launch()

        for name, config in screenshots.items():
            capture_screenshot(browser, name, config, base_url)

        browser.close()

    print(f"\n✅ Done! Captured {len(screenshots)} screenshots.\n")
    return True

def validate(manifest: dict):
    """Check all output files exist and have reasonable size."""
    print("\n🔍 Validating screenshots...\n")

    all_valid = True
    for name, config in manifest['screenshots'].items():
        output_path = Path('apps/zerg/frontend-web') / config['output']

        if not output_path.exists():
            print(f"  ✗ {name}: MISSING")
            all_valid = False
            continue

        size_kb = output_path.stat().st_size / 1024
        if size_kb < 10:
            print(f"  ✗ {name}: TOO SMALL ({size_kb:.0f} KB)")
            all_valid = False
        elif size_kb > 2000:
            print(f"  ⚠ {name}: LARGE ({size_kb:.0f} KB)")
        else:
            print(f"  ✓ {name}: OK ({size_kb:.0f} KB)")

    print()
    return all_valid

def list_screenshots(manifest: dict):
    """List available screenshots."""
    print("\n📋 Available screenshots:\n")
    for name, config in manifest['screenshots'].items():
        print(f"  {name:20s} {config.get('description', '')}")
    print()

def main():
    parser = argparse.ArgumentParser(description='Capture marketing screenshots')
    parser.add_argument('--name', '-n', action='append', help='Capture specific screenshot(s)')
    parser.add_argument('--list', '-l', action='store_true', help='List available screenshots')
    parser.add_argument('--validate', '-v', action='store_true', help='Validate existing screenshots')
    args = parser.parse_args()

    manifest = load_manifest()

    if args.list:
        list_screenshots(manifest)
        return 0

    if args.validate:
        return 0 if validate(manifest) else 1

    success = capture_all(manifest, args.name)
    return 0 if success else 1

if __name__ == '__main__':
    sys.exit(main())
```

### Seed Script Updates

Update `apps/zerg/backend/scripts/seed_marketing_workflow.py`:

1. Use **stable names** for workflows: "health", "inbox", "home"
2. Use **stable name** for chat thread: "marketing"
3. Add `name` field to workflows (not just `title`)

```python
HEALTH_WORKFLOW = {
    "name": "health",  # Used for URL lookup
    "title": "Morning Health Check",
    # ...
}

CHAT_THREAD = {
    "name": "marketing",  # Used for URL lookup
    "title": "Marketing Demo Chat",
    # ...
}
```

### Make Targets

Add to `Makefile`:

```makefile
# ============================================================================
# Marketing Screenshots
# ============================================================================

.PHONY: marketing-capture marketing-single marketing-validate marketing-seed

marketing-capture: ## Capture all marketing screenshots
	@echo "📸 Capturing marketing screenshots..."
	@$(call check_dev_stack)
	@cd $(ROOT_DIR) && uv run scripts/capture_marketing.py

marketing-single: ## Capture single screenshot (NAME=chat-preview)
	@$(call check_dev_stack)
	@cd $(ROOT_DIR) && uv run scripts/capture_marketing.py --name $(NAME)

marketing-validate: ## Validate all marketing screenshots exist
	@cd $(ROOT_DIR) && uv run scripts/capture_marketing.py --validate

marketing-list: ## List available marketing screenshots
	@cd $(ROOT_DIR) && uv run scripts/capture_marketing.py --list

marketing-seed: ## Re-seed marketing data only
	@$(call check_dev_stack)
	@cd $(ROOT_DIR) && uv run apps/zerg/backend/scripts/seed_marketing_workflow.py
```

---

## Implementation Tasks

### Phase 1: URL Addressability

#### Task 1.1: Workflow name resolution
**File:** `apps/zerg/frontend-web/src/pages/CanvasPage.tsx`

- Read `workflow` param from URL: `const [searchParams] = useSearchParams()`
- If param is numeric, use as ID (existing behavior)
- If param is string, call API to resolve: `GET /api/workflows?name={name}`
- Load resolved workflow on mount

**Acceptance criteria:**
- [ ] `/canvas?workflow=health` loads the health workflow
- [ ] `/canvas?workflow=3` still works (ID fallback)
- [ ] Invalid name shows error state

#### Task 1.2: Thread name resolution
**File:** `apps/zerg/frontend-web/src/jarvis/app/App.tsx` (or thread context)

- Read `thread` param from URL
- If param exists, resolve by name: `GET /api/threads?name={name}`
- Auto-select resolved thread on mount

**Acceptance criteria:**
- [ ] `/chat?thread=marketing` loads and selects the marketing thread
- [ ] Messages render automatically
- [ ] Invalid name shows thread list (graceful fallback)

#### Task 1.3: Backend name resolution endpoints
**Files:**
- `apps/zerg/backend/zerg/routers/workflows.py`
- `apps/zerg/backend/zerg/routers/threads.py`

Add query param support:
```python
@router.get("/workflows")
def get_workflows(name: str | None = None):
    if name:
        return crud.get_workflow_by_name(db, name)
    return crud.get_workflows(db)
```

**Acceptance criteria:**
- [ ] `GET /api/workflows?name=health` returns single workflow
- [ ] `GET /api/threads?name=marketing` returns single thread
- [ ] Returns 404 if name not found

### Phase 2: Marketing Mode

#### Task 2.1: Marketing CSS file
**File:** `apps/zerg/frontend-web/src/styles/marketing-mode.css`

Extract vivid styles from `scripts/capture_marketing_screenshots.py`:
- Canvas node glows (purple agents, amber triggers)
- Edge glows (blue/cyan gradients)
- Hidden chrome (minimap, controls, sidebars)
- Enhanced backgrounds

**Acceptance criteria:**
- [ ] File contains all vivid styles
- [ ] Styles scoped under `body.marketing-mode`
- [ ] No visual change when class not applied

#### Task 2.2: Marketing mode toggle
**File:** `apps/zerg/frontend-web/src/App.tsx` (or layout)

```tsx
const [searchParams] = useSearchParams();
const isMarketing = searchParams.get('marketing') === 'true';

useEffect(() => {
  if (isMarketing) {
    document.body.classList.add('marketing-mode');
  }
  return () => document.body.classList.remove('marketing-mode');
}, [isMarketing]);
```

**Acceptance criteria:**
- [ ] `?marketing=true` applies marketing-mode class
- [ ] Vivid styling visible in browser
- [ ] Class removed when navigating away

### Phase 3: Ready Signals

#### Task 3.1: Canvas ready signal
**File:** `apps/zerg/frontend-web/src/pages/CanvasPage.tsx`

```tsx
useEffect(() => {
  const ready = workflowLoaded && nodes.length > 0;
  if (ready) {
    document.body.setAttribute('data-ready', 'true');
  }
  return () => document.body.removeAttribute('data-ready');
}, [workflowLoaded, nodes]);
```

**Acceptance criteria:**
- [ ] `data-ready="true"` set when workflow fully rendered
- [ ] Attribute removed on unmount
- [ ] Works with marketing mode

#### Task 3.2: Chat ready signal
**File:** `apps/zerg/frontend-web/src/jarvis/app/App.tsx` (or chat view)

**Acceptance criteria:**
- [ ] `data-ready="true"` set when messages rendered
- [ ] Works when thread auto-selected via URL

#### Task 3.3: Dashboard ready signal
**File:** `apps/zerg/frontend-web/src/pages/DashboardPage.tsx`

**Acceptance criteria:**
- [ ] `data-ready="true"` set when agents loaded
- [ ] Works with marketing mode

### Phase 4: Seed Updates

#### Task 4.1: Stable workflow names
**File:** `apps/zerg/backend/scripts/seed_marketing_workflow.py`

- Add `name` field to each workflow: "health", "inbox", "home"
- Ensure names are unique and stable across re-seeds

**Acceptance criteria:**
- [ ] Workflows have `name` field
- [ ] Names persist across re-runs
- [ ] API can look up by name

#### Task 4.2: Stable thread name
**File:** `apps/zerg/backend/scripts/seed_marketing_workflow.py`

- Add `name` field to marketing thread: "marketing"

**Acceptance criteria:**
- [ ] Thread has `name` field
- [ ] API can look up by name

### Phase 5: Manifest & Capture Script

#### Task 5.1: Screenshot manifest
**File:** `scripts/screenshots.yaml`

Create manifest with all 6 screenshots (see Architecture section above).

**Acceptance criteria:**
- [ ] YAML file with all screenshot definitions
- [ ] Valid viewport and crop dimensions
- [ ] Correct output paths

#### Task 5.2: New capture script
**File:** `scripts/capture_marketing.py`

Replace existing script with manifest-driven version (see Architecture section).

**Acceptance criteria:**
- [ ] Reads from manifest
- [ ] Waits for ready signal
- [ ] Supports `--name`, `--list`, `--validate` flags
- [ ] No CSS injection
- [ ] No clicking/UI manipulation

#### Task 5.3: Make targets
**File:** `Makefile`

Add marketing screenshot targets (see Architecture section).

**Acceptance criteria:**
- [ ] `make marketing-capture` works
- [ ] `make marketing-single NAME=x` works
- [ ] `make marketing-validate` works

### Phase 6: Cleanup

#### Task 6.1: Remove old capture script
- Delete or archive `scripts/capture_marketing_screenshots.py`
- Update `scripts/README_SCREENSHOTS.md`

#### Task 6.2: Update documentation
- Update `AGENTS.md` with new make targets
- Document URL params for marketing mode

---

## Testing

### Manual Testing Checklist

After implementation, verify:

1. **URL addressability**
   - [ ] Open `/canvas?workflow=health` → Health workflow loads
   - [ ] Open `/chat?thread=marketing` → Marketing thread selected, messages visible
   - [ ] Open `/dashboard?marketing=true` → Dashboard with vivid styling

2. **Ready signals**
   - [ ] DevTools shows `data-ready="true"` on body when pages load
   - [ ] Attribute appears after content renders, not immediately

3. **Marketing mode**
   - [ ] `?marketing=true` applies vivid styling
   - [ ] Canvas nodes glow, edges glow, chrome hidden
   - [ ] Styling works on all three pages

4. **Capture script**
   - [ ] `make marketing-capture` produces 6 screenshots
   - [ ] Screenshots match expected dimensions
   - [ ] No errors or warnings about ready signal

5. **End-to-end**
   - [ ] Fresh DB → `make marketing-seed` → `make marketing-capture` → All 6 screenshots valid

---

## Success Criteria

- [ ] Capture script is <50 lines of Python
- [ ] No Playwright clicking or UI manipulation
- [ ] No CSS injection
- [ ] No arbitrary sleeps (only ready signal wait)
- [ ] Screenshots reproducible: same input → same output
- [ ] Any screenshot capturable individually via `make marketing-single NAME=x`
- [ ] Future agents can understand system in <5 minutes

---

## Files Changed

| File | Change |
|------|--------|
| `apps/zerg/frontend-web/src/pages/CanvasPage.tsx` | URL params, ready signal |
| `apps/zerg/frontend-web/src/jarvis/app/App.tsx` | URL params, ready signal |
| `apps/zerg/frontend-web/src/pages/DashboardPage.tsx` | Ready signal |
| `apps/zerg/frontend-web/src/App.tsx` | Marketing mode toggle |
| `apps/zerg/frontend-web/src/styles/marketing-mode.css` | New file |
| `apps/zerg/backend/zerg/routers/workflows.py` | Name query param |
| `apps/zerg/backend/zerg/routers/threads.py` | Name query param |
| `apps/zerg/backend/scripts/seed_marketing_workflow.py` | Stable names |
| `scripts/screenshots.yaml` | New file |
| `scripts/capture_marketing.py` | New file (replaces old) |
| `Makefile` | New targets |

---

## Future Enhancements (Out of Scope)

- Preview page (`/marketing-preview`) with iframe grid
- Hot reload when manifest changes
- CI integration to validate screenshots haven't drifted
- Automatic image optimization (pngquant, etc.)
