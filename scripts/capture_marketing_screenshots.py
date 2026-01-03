#!/usr/bin/env python3
"""
Capture marketing-ready screenshots of all key app screens.

This script:
1. Seeds marketing data (3 workflows, agents, runs, chat history)
2. Opens each page using Playwright
3. Applies "vivid mode" CSS for marketing appeal
4. Takes high-quality screenshots of REAL app pages
5. Saves to the landing page assets directory

Screenshots captured:
- Canvas workflow previews (health, inbox, home - one for each scenario card)
- Chat interface with real conversation
- Dashboard with agents
- Full canvas preview

Requirements:
    - Dev stack running at localhost:30080 (make dev)
    - Playwright installed (bunx playwright install chromium)

Usage:
    python scripts/capture_marketing_screenshots.py
"""

import asyncio
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

# Add backend to path for imports
backend_dir = Path(__file__).parent.parent / "apps" / "zerg" / "backend"
sys.path.insert(0, str(backend_dir))

# Output directory for all screenshots
OUTPUT_DIR = Path(__file__).parent.parent / "apps" / "zerg" / "frontend-web" / "public" / "images" / "landing"
BASE_URL = "http://localhost:30080"

# ============================================================================
# VIVID MODE CSS - Marketing-ready styling
# ============================================================================

CANVAS_VIVID_CSS = """
/* Hide chrome for clean screenshot */
#agent-shelf, .react-flow__minimap, .react-flow__controls,
.execution-controls, .status-footer, .canvas-controls-panel { display: none !important; }

/* Vibrant background */
.react-flow {
  background: linear-gradient(145deg, #070b14 0%, #0c1222 50%, #111827 100%) !important;
}

/* Agent nodes - purple glow */
.agent-node {
  background: linear-gradient(135deg, #1e1b4b 0%, #312e81 100%) !important;
  border: 2px solid #818cf8 !important;
  border-left: 5px solid #a78bfa !important;
  box-shadow: 0 0 20px rgba(139, 92, 246, 0.5), 0 0 40px rgba(139, 92, 246, 0.3), 0 8px 32px rgba(0, 0, 0, 0.4) !important;
}

/* Trigger node - amber glow */
.trigger-node {
  background: linear-gradient(135deg, #451a03 0%, #78350f 100%) !important;
  border: 2px solid #fbbf24 !important;
  border-left: 5px solid #fcd34d !important;
  box-shadow: 0 0 20px rgba(251, 191, 36, 0.5), 0 0 40px rgba(251, 191, 36, 0.3), 0 8px 32px rgba(0, 0, 0, 0.4) !important;
}

/* Glowing edges */
.react-flow__edge-path {
  stroke: #60a5fa !important;
  stroke-width: 2.5px !important;
  filter: drop-shadow(0 0 4px rgba(96, 165, 250, 0.6)) !important;
}

/* Bright text */
.agent-name, .trigger-name, .tool-name {
  color: #ffffff !important;
  font-weight: 600 !important;
  text-shadow: 0 0 10px rgba(255,255,255,0.3) !important;
}

/* Glowing handles */
.react-flow__handle {
  background: #a78bfa !important;
  border: 2px solid #fff !important;
  box-shadow: 0 0 8px rgba(167, 139, 250, 0.8) !important;
}
"""

CHAT_VIVID_CSS = """
/* Hide thread sidebar for cleaner look */
.threads-panel, .thread-sidebar { width: 0 !important; min-width: 0 !important; opacity: 0 !important; }
.chat-main { flex: 1 !important; }

/* Boost background gradient */
.chat-view-container, .chat-main, .jarvis-chat-view {
  background: linear-gradient(145deg, #0a0f1e 0%, #0f1729 50%, #111827 100%) !important;
}

/* Enhance message styling */
.message-container {
  max-width: 900px !important;
  margin: 0 auto !important;
}

/* User messages - blue glow */
.user-message .message-content, .message-bubble.user {
  background: linear-gradient(135deg, #1e40af 0%, #1e3a8a 100%) !important;
  border: 1px solid #3b82f6 !important;
  box-shadow: 0 4px 12px rgba(59, 130, 246, 0.3) !important;
}

/* Assistant messages - purple glow */
.assistant-message .message-content, .message-bubble.assistant {
  background: linear-gradient(135deg, #4c1d95 0%, #5b21b6 100%) !important;
  border: 1px solid #8b5cf6 !important;
  box-shadow: 0 4px 12px rgba(139, 92, 246, 0.3) !important;
}

/* Tool use indicators */
.tool-use, .tool-result, .tool-indicator {
  background: linear-gradient(135deg, #064e3b 0%, #065f46 100%) !important;
  border: 1px solid #10b981 !important;
  box-shadow: 0 0 8px rgba(16, 185, 129, 0.4) !important;
}

/* Text clarity */
.message-text, .message-content {
  color: #f3f4f6 !important;
}
"""

DASHBOARD_VIVID_CSS = """
/* Enhance agent cards */
.agent-card, .dashboard-card {
  background: linear-gradient(135deg, #1e1b4b 0%, #312e81 100%) !important;
  border: 2px solid #818cf8 !important;
  box-shadow: 0 8px 24px rgba(139, 92, 246, 0.4) !important;
}

/* Status indicators */
.status-active, .status-badge.active, .badge-running {
  background: #10b981 !important;
  box-shadow: 0 0 12px rgba(16, 185, 129, 0.6) !important;
}

.status-idle, .status-badge.idle, .badge-idle {
  background: #6b7280 !important;
}

/* Boost dashboard background */
.dashboard-container, .dashboard-page {
  background: linear-gradient(145deg, #070b14 0%, #0c1222 50%, #111827 100%) !important;
}

/* Card titles pop */
.card-title, .agent-name, .dashboard-agent-name {
  color: #f3f4f6 !important;
  text-shadow: 0 0 8px rgba(255,255,255,0.2) !important;
}
"""


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================


async def seed_marketing_data():
    """Run the seed script to populate marketing data."""
    print("🌱 Seeding marketing data...")

    seed_script = backend_dir / "scripts" / "seed_marketing_workflow.py"
    result = subprocess.run(
        ["uv", "run", "python", str(seed_script)],
        cwd=backend_dir,
        capture_output=True,
        text=True
    )

    if result.returncode != 0:
        print(f"❌ Failed to seed data:")
        print(result.stderr)
        sys.exit(1)

    print(result.stdout)


def api_request(path: str, method: str = "GET", data: dict = None) -> dict | list | None:
    """Make an API request. Handles FastAPI's trailing slash requirements."""
    # GET needs trailing slash (FastAPI redirect strips path otherwise)
    # PATCH/POST/etc work without trailing slash
    if method == "GET" and not path.endswith("/"):
        path = path + "/"
    url = f"{BASE_URL}/api{path}"
    req = urllib.request.Request(url, method=method)
    req.add_header("Content-Type", "application/json")

    if data:
        req.data = json.dumps(data).encode()

    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read())
    except Exception as e:
        print(f"  Warning: API request failed ({method} {path}): {e}")
        return None


def get_workflow_by_name(name: str) -> dict | None:
    """Fetch workflow by name via API."""
    workflows = api_request("/workflows")
    if workflows:
        for wf in workflows:
            if wf["name"] == name:
                return wf
    return None


def set_current_workflow(workflow_id: int):
    """Update a workflow to make it the 'current' one by touching it."""
    workflows = api_request("/workflows")
    if not workflows:
        print(f"  Warning: No workflows found")
        return

    target_wf = None
    for wf in workflows:
        if wf["id"] == workflow_id:
            target_wf = wf
            break

    if target_wf:
        # Touch the workflow to update its timestamp
        result = api_request(f"/workflows/{workflow_id}", method="PATCH", data={"name": target_wf["name"]})
        if result:
            print(f"  Set current workflow to: {target_wf['name']}")
    else:
        print(f"  Warning: Workflow ID {workflow_id} not found")


def get_chat_thread_id() -> int | None:
    """Get the marketing demo chat thread ID."""
    threads = api_request("/threads")
    if threads:
        for thread in threads:
            if thread.get("title") == "Marketing Demo Chat":
                return thread["id"]
    return None


async def capture_canvas_screenshot(browser, workflow_name: str, output_name: str, crop: dict = None):
    """Capture a canvas workflow screenshot by navigating directly to the workflow ID."""
    print(f"\n📸 Capturing canvas: {workflow_name}...")

    # Get workflow ID
    workflow = get_workflow_by_name(workflow_name)
    if not workflow:
        print(f"  Warning: Workflow '{workflow_name}' not found")
        return

    workflow_id = workflow["id"]

    # Create fresh page
    page = await browser.new_page(viewport={"width": 1400, "height": 900})

    # Navigate to canvas with specific workflow ID
    await page.goto(f"{BASE_URL}/canvas?workflow={workflow_id}&log=minimal")

    # Wait for React Flow to load
    try:
        await page.wait_for_selector(".react-flow__node", timeout=10000)
    except Exception:
        print(f"  Warning: No nodes found for {workflow_name}")

    await asyncio.sleep(2)

    # Apply vivid CSS
    await page.add_style_tag(content=CANVAS_VIVID_CSS)
    await asyncio.sleep(0.5)

    # Fit view
    await page.evaluate("""
        () => {
            const reactFlowInstance = window.__reactFlowInstance;
            if (reactFlowInstance && reactFlowInstance.fitView) {
                reactFlowInstance.fitView({ maxZoom: 1.2, duration: 0, padding: 0.1 });
            }
        }
    """)
    await asyncio.sleep(0.5)

    # Take screenshot
    output_path = OUTPUT_DIR / output_name

    if crop:
        await page.screenshot(path=str(output_path), clip=crop)
    else:
        # Screenshot the canvas workspace
        canvas_element = await page.query_selector(".canvas-workspace")
        if canvas_element:
            await canvas_element.screenshot(path=str(output_path))
        else:
            await page.screenshot(path=str(output_path), full_page=False)

    size_kb = output_path.stat().st_size / 1024
    print(f"  ✅ Saved: {output_name} ({size_kb:.1f} KB)")

    # Close the page to free resources
    await page.close()


async def capture_chat_screenshot(browser):
    """Capture chat interface with real conversation."""
    print("\n📸 Capturing chat screenshot...")

    # Create fresh page
    page = await browser.new_page(viewport={"width": 1400, "height": 900})

    # Get the marketing thread ID
    thread_id = get_chat_thread_id()

    # Navigate to chat with the specific thread
    if thread_id:
        await page.goto(f"{BASE_URL}/chat/{thread_id}?log=minimal")
    else:
        await page.goto(f"{BASE_URL}/chat?log=minimal")
        print("  Warning: Marketing Demo Chat thread not found, using default")

    # Wait for chat to load
    await asyncio.sleep(3)

    # Wait for messages to appear
    try:
        await page.wait_for_selector(".message-container, .chat-message, .jarvis-message, .message-bubble", timeout=8000)
    except Exception:
        print("  Warning: No messages found in chat")

    await asyncio.sleep(1)

    # Apply vivid CSS
    await page.add_style_tag(content=CHAT_VIVID_CSS)
    await asyncio.sleep(0.5)

    # Take screenshot
    output_path = OUTPUT_DIR / "chat-preview.png"
    await page.screenshot(path=str(output_path), full_page=False)

    size_kb = output_path.stat().st_size / 1024
    print(f"  ✅ Saved: chat-preview.png ({size_kb:.1f} KB)")

    await page.close()


async def capture_dashboard_screenshot(browser):
    """Capture dashboard with agents."""
    print("\n📸 Capturing dashboard screenshot...")

    # Create fresh page
    page = await browser.new_page(viewport={"width": 1400, "height": 900})

    await page.goto(f"{BASE_URL}/dashboard?log=minimal&effects=off")

    # Wait for dashboard to load
    await asyncio.sleep(2)

    # Wait for agent cards to appear
    try:
        await page.wait_for_selector(".agent-card, .dashboard-card, .agent-row", timeout=8000)
    except Exception:
        print("  Warning: No agent cards found")

    await asyncio.sleep(1)

    # Apply vivid CSS
    await page.add_style_tag(content=DASHBOARD_VIVID_CSS)
    await asyncio.sleep(0.5)

    # Take screenshot
    output_path = OUTPUT_DIR / "dashboard-preview.png"
    await page.screenshot(path=str(output_path), full_page=False)

    size_kb = output_path.stat().st_size / 1024
    print(f"  ✅ Saved: dashboard-preview.png ({size_kb:.1f} KB)")

    await page.close()


async def capture_all_screenshots():
    """Capture all marketing screenshots."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("❌ Playwright not installed. Installing...")
        subprocess.run(["bunx", "playwright", "install", "chromium"], check=True)
        from playwright.async_api import async_playwright

    print("🌐 Launching browser...")

    # Ensure output directory exists
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch()

        # Capture scenario screenshots (800x500 crops)
        # Each capture creates a fresh page to avoid React Query cache issues
        scenario_crop = {"x": 100, "y": 100, "width": 800, "height": 500}

        await capture_canvas_screenshot(
            browser,
            "Morning Health Check",
            "scenario-health.png",
            crop=scenario_crop
        )

        await capture_canvas_screenshot(
            browser,
            "Email Automation Pipeline",
            "scenario-inbox.png",
            crop=scenario_crop
        )

        await capture_canvas_screenshot(
            browser,
            "Smart Home Automation",
            "scenario-home.png",
            crop=scenario_crop
        )

        # Capture full canvas preview (1400x900)
        await capture_canvas_screenshot(
            browser,
            "Email Automation Pipeline",
            "canvas-preview.png"
        )

        # Capture chat screenshot
        await capture_chat_screenshot(browser)

        # Capture dashboard screenshot
        await capture_dashboard_screenshot(browser)

        await browser.close()

    print("\n✅ All screenshots captured!")


async def main(skip_seed: bool = False):
    """Main execution flow."""
    print("🚀 Marketing Screenshot Capture")
    print("=" * 60)
    print()

    # Check if dev stack is running
    print("🔍 Checking dev stack...")
    try:
        urllib.request.urlopen(f"{BASE_URL}/health", timeout=2)
        print("  ✓ Dev stack is running")
    except Exception:
        print("❌ Dev stack not running. Please run 'make dev' first.")
        sys.exit(1)

    print()

    # Seed marketing data (unless already seeded via Makefile)
    if not skip_seed:
        await seed_marketing_data()
        print()
        # Wait for data to settle
        await asyncio.sleep(2)
    else:
        print("⏭️  Skipping seed (--skip-seed flag)")
        await asyncio.sleep(1)  # Brief pause for any DB writes to settle

    # Capture screenshots
    await capture_all_screenshots()
    print()

    # Summary
    print("🎉 Marketing screenshot capture complete!")
    print()
    print(f"📁 Output directory: {OUTPUT_DIR}")
    print()
    print("📸 Screenshots generated:")
    target_files = ["scenario-health.png", "scenario-inbox.png", "scenario-home.png",
                    "canvas-preview.png", "chat-preview.png", "dashboard-preview.png"]
    for screenshot in sorted(OUTPUT_DIR.glob("*.png")):
        if screenshot.name in target_files:
            size_kb = screenshot.stat().st_size / 1024
            print(f"   {screenshot.name:30s} {size_kb:8.1f} KB")
    print()
    print("💡 Next steps:")
    print("   1. Review screenshots in the output directory")
    print("   2. Commit changes: git add " + str(OUTPUT_DIR.relative_to(Path.cwd())))


if __name__ == "__main__":
    skip_seed = "--skip-seed" in sys.argv
    asyncio.run(main(skip_seed=skip_seed))
