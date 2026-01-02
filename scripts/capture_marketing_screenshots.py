#!/usr/bin/env python3
"""
Capture marketing-ready screenshots of the canvas workflow.

This script:
1. Seeds the marketing workflow
2. Opens the canvas page using Playwright
3. Applies "vivid mode" CSS for marketing appeal
4. Takes a high-quality screenshot
5. Saves to the landing page assets directory

Usage:
    python scripts/capture_marketing_screenshots.py

Requirements:
    - Dev stack running at localhost:30080 (make dev)
    - Playwright installed (bunx playwright install chromium)
"""

import asyncio
import sys
import subprocess
from pathlib import Path

# Add backend to path for imports
backend_dir = Path(__file__).parent.parent / "apps" / "zerg" / "backend"
sys.path.insert(0, str(backend_dir))

# Marketing CSS for vibrant, glowing nodes
VIVID_MODE_CSS = """
/* Hide chrome */
#agent-shelf, .react-flow__minimap, .react-flow__controls,
.execution-controls, .status-footer { display: none !important; }

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


async def seed_workflow():
    """Seed the marketing workflow using the existing script."""
    print("🌱 Seeding marketing workflow...")
    seed_script = backend_dir / "scripts" / "seed_marketing_workflow.py"

    result = subprocess.run(
        ["uv", "run", "python", str(seed_script)],
        cwd=backend_dir,
        capture_output=True,
        text=True
    )

    if result.returncode != 0:
        print(f"❌ Failed to seed workflow:")
        print(result.stderr)
        sys.exit(1)

    print(result.stdout)


async def capture_screenshot():
    """Capture the canvas screenshot with vivid styling."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("❌ Playwright not installed. Installing...")
        import subprocess
        subprocess.run(["uv", "tool", "install", "playwright"], check=True)
        subprocess.run(["uv", "tool", "run", "playwright", "install", "chromium"], check=True)
        from playwright.async_api import async_playwright

    print("📸 Launching browser...")

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1400, "height": 900})

        print("🌐 Navigating to canvas page...")
        await page.goto("http://localhost:30080/canvas")

        # Wait for canvas to load
        print("⏳ Waiting for workflow to load...")
        await page.wait_for_selector(".react-flow__node", timeout=10000)

        # Give it a moment for all nodes to render
        await asyncio.sleep(2)

        # Inject vivid mode CSS
        print("🎨 Applying vivid mode styling...")
        await page.add_style_tag(content=VIVID_MODE_CSS)

        # Wait for CSS to apply
        await asyncio.sleep(1)

        # Fit view to show all nodes
        print("🔍 Fitting view...")
        await page.evaluate("""
            () => {
                // Try to trigger fitView if available
                const reactFlowInstance = window.__reactFlowInstance;
                if (reactFlowInstance && reactFlowInstance.fitView) {
                    reactFlowInstance.fitView({ maxZoom: 1, duration: 0 });
                }
            }
        """)

        await asyncio.sleep(1)

        # Take screenshot of the canvas workspace
        print("📷 Capturing screenshot...")
        canvas_element = await page.query_selector(".canvas-workspace")
        if not canvas_element:
            print("❌ Canvas workspace not found")
            await browser.close()
            sys.exit(1)

        output_dir = Path(__file__).parent.parent / "apps" / "zerg" / "frontend-web" / "public" / "images" / "landing"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / "canvas-preview.png"

        await canvas_element.screenshot(path=str(output_path))

        print(f"✅ Screenshot saved to: {output_path}")
        print(f"   Size: {output_path.stat().st_size / 1024:.1f} KB")

        await browser.close()

    return output_path


async def main():
    """Main execution flow."""
    print("🚀 Marketing Screenshot Capture")
    print("=" * 50)
    print()

    # Check if dev stack is running
    print("🔍 Checking if dev stack is running...")
    import urllib.request
    try:
        urllib.request.urlopen("http://localhost:30080/health", timeout=2)
        print("✅ Dev stack is running")
    except Exception:
        print("❌ Dev stack not running. Please run 'make dev' first.")
        sys.exit(1)

    print()

    # Seed workflow
    await seed_workflow()
    print()

    # Capture screenshot
    output_path = await capture_screenshot()
    print()

    print("🎉 Marketing screenshot capture complete!")
    print()
    print(f"📁 Output: {output_path}")
    print()
    print("💡 Next steps:")
    print("   1. Review the screenshot")
    print("   2. Use it on the landing page")
    print("   3. Commit to git: git add " + str(output_path.relative_to(Path.cwd())))


if __name__ == "__main__":
    asyncio.run(main())
