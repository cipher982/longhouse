# Marketing Screenshot Capture System

## Quick Start

```bash
# 1. Start the dev stack
make dev

# 2. Capture all marketing screenshots (in another terminal)
make screenshot-marketing
```

That's it! One command seeds all data and captures all screenshots.

## What It Does

### 1. Seeds Marketing Data
Creates realistic data for professional screenshots:

**Three distinct workflows:**
- **Morning Health Check** - WHOOP → Analyzer → Notifier (health scenario)
- **Email Automation Pipeline** - 6-node email triage workflow (inbox scenario)
- **Smart Home Automation** - Location → Presence → Lights/Thermostat (home scenario)

**Agents with varied statuses:**
- Some running, some idle, some recently completed
- Realistic timestamps and token counts

**Chat conversation:**
- Marketing Demo Chat thread with Jarvis
- Shows tool use (WHOOP data, location check)
- Multi-turn conversation about health + activities

### 2. Captures Screenshots

| Screenshot | Content | Dimensions |
|------------|---------|------------|
| `scenario-health.png` | Morning Health Check workflow | 800×500 |
| `scenario-inbox.png` | Email Automation Pipeline | 800×500 |
| `scenario-home.png` | Smart Home Automation | 800×500 |
| `canvas-preview.png` | Full canvas view | 1400×900 |
| `chat-preview.png` | Real Jarvis conversation | 1400×900 |
| `dashboard-preview.png` | Dashboard with agents | 1400×900 |

### 3. Applies "Vivid Mode" CSS
Marketing-ready styling injected via Playwright:
- Purple glowing agent nodes
- Amber glowing trigger nodes
- Blue glowing connection edges
- Dark gradient backgrounds
- Hidden UI chrome (minimap, controls, sidebar)

## Output Location

```
apps/zerg/frontend-web/public/images/landing/
├── scenario-health.png   # Health workflow (800×500)
├── scenario-inbox.png    # Inbox workflow (800×500)
├── scenario-home.png     # Home workflow (800×500)
├── canvas-preview.png    # Full canvas (1400×900)
├── chat-preview.png      # Chat interface (1400×900)
└── dashboard-preview.png # Dashboard (1400×900)
```

## How It Works

```
make screenshot-marketing
         │
         ├─→ Check dev stack is running
         │
         ├─→ Run seed_marketing_workflow.py
         │   └─→ Creates 3 workflows, 13 agents, runs, chat thread
         │
         └─→ Run capture_marketing_screenshots.py
             ├─→ For each workflow: set as current, capture
             ├─→ Navigate to chat with seeded thread
             ├─→ Navigate to dashboard
             └─→ Apply vivid CSS + take screenshots
```

## Idempotency

Safe to run repeatedly:
- Seed script cleans up old marketing data before creating new
- Screenshots overwrite previous files
- No manual cleanup required

## Customization

### Modify Workflow Layouts
Edit `apps/zerg/backend/scripts/seed_marketing_workflow.py`:
- `HEALTH_WORKFLOW`, `INBOX_WORKFLOW`, `HOME_WORKFLOW` dicts
- Adjust `layout` coordinates for node positions
- Modify `edges` for different connections

### Adjust Styling
Edit `scripts/capture_marketing_screenshots.py`:
- `CANVAS_VIVID_CSS` - Canvas node/edge styling
- `CHAT_VIVID_CSS` - Chat message styling
- `DASHBOARD_VIVID_CSS` - Dashboard card styling

### Change Viewport Size
In `capture_marketing_screenshots.py`:
```python
page = await browser.new_page(viewport={"width": 1400, "height": 900})
```

### Change Crop Dimensions
```python
scenario_crop = {"x": 100, "y": 100, "width": 800, "height": 500}
```

## Requirements

- Dev stack running at localhost:30080 (`make dev`)
- UV (Python package manager)
- Playwright (auto-installed if missing)

## Troubleshooting

**Dev stack not running:**
```bash
make dev-bg  # Start in background
```

**Playwright not found:**
```bash
bunx playwright install chromium
```

**Canvas not loading:**
- Check backend logs: `make logs`
- Verify health: `curl http://localhost:30080/health`
- Wait a few seconds after `make dev` for services to initialize

**Screenshots look wrong:**
- Check browser viewport: 1400×900 is optimal
- Verify CSS selectors match current UI (may need updates after refactors)

## Scripts

| File | Purpose |
|------|---------|
| `scripts/capture_marketing_screenshots.py` | Main capture script |
| `apps/zerg/backend/scripts/seed_marketing_workflow.py` | Data seeding |
| `scripts/README_SCREENSHOTS.md` | This documentation |

## Make Target

```makefile
screenshot-marketing: ## Capture all marketing screenshots
    @# 1. Check dev stack
    @# 2. Seed marketing data
    @# 3. Capture screenshots via Playwright
```
