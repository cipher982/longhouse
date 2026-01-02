# Marketing Screenshot Capture System

## Quick Start

```bash
# 1. Start the dev stack
make dev

# 2. Capture marketing screenshots (in another terminal)
make screenshot-marketing
```

## What It Does

1. **Seeds the marketing workflow** - Creates a visually appealing "Email Automation Pipeline" workflow with 6 agents and a trigger
2. **Opens the canvas page** - Navigates to http://localhost:30080/canvas
3. **Applies vivid mode CSS** - Injects marketing-ready styling:
   - Hide UI chrome (shelf, minimap, controls)
   - Vibrant gradient background
   - Purple glowing agent nodes
   - Amber glowing trigger node
   - Blue glowing connection edges
   - Enhanced text with shadows
4. **Takes high-quality screenshot** - Captures at 1400x900 viewport
5. **Saves to landing page assets** - Output: `apps/zerg/frontend-web/public/images/landing/canvas-preview.png`

## Output

The screenshot showcases the Email Automation Pipeline workflow:
- **Trigger**: "New Email" (amber glow)
- **Agents**: Email Watcher → Content Analyzer → Priority Router → Slack Notifier / Task Creator → Calendar Checker (purple glows)
- **Background**: Dark gradient (professional, modern look)
- **Edges**: Blue glowing connections showing data flow

## Requirements

- Dev stack running at localhost:30080
- UV (Python package manager)
- Playwright (auto-installed if missing)

## Script Details

- **Location**: `scripts/capture_marketing_screenshots.py`
- **Language**: Python with async Playwright
- **Dependencies**: Automatically installed via `uv run --with playwright`
- **Make Target**: `make screenshot-marketing`

## Customization

To modify the screenshot:

1. **Change workflow layout**: Edit `apps/zerg/backend/scripts/seed_marketing_workflow.py`
2. **Adjust styling**: Modify `VIVID_MODE_CSS` in `scripts/capture_marketing_screenshots.py`
3. **Change viewport size**: Update `viewport={"width": 1400, "height": 900}` in the script

## Idempotency

The system is designed to be run multiple times safely:
- Workflow seeding updates existing workflow (doesn't create duplicates)
- Screenshot file is overwritten each time
- No manual cleanup required

## Troubleshooting

**Dev stack not running:**
```bash
make dev-bg  # Start in background
```

**Playwright not found:**
The script auto-installs Playwright via UV. If issues persist:
```bash
uv tool install playwright
uv tool run playwright install chromium
```

**Canvas not loading:**
- Check backend logs: `make logs`
- Verify health: `curl http://localhost:30080/health`
- Wait a few seconds after `make dev` for services to initialize
