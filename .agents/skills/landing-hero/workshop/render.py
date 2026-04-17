#!/usr/bin/env python3
"""Render each variant by bounding-box clip."""
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

ROOT = Path(__file__).parent
URL = f"file://{ROOT}/variants.html"
# Default = the current finalists. Edit this list to render a subset or add new ids.
VARIANTS = ["va", "vf", "vg", "vj", "vk"]
# Pin chromium to avoid playwright-version-vs-cached-chromium drift. If this
# path doesn't exist on your machine, run:
#   uv run --with "playwright==1.50.0" playwright install chromium
# then point EXEC at one of ~/Library/Caches/ms-playwright/chromium-*/chrome-mac-arm64/Google Chrome for Testing.app/...
EXEC = "/Users/davidrose/Library/Caches/ms-playwright/chromium-1200/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"


async def render():
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=EXEC)
        ctx = await browser.new_context(viewport={"width": 1440, "height": 1500}, device_scale_factor=2)
        page = await ctx.new_page()
        await page.goto(URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)
        for v in VARIANTS:
            # Scroll section into view at very top, then read bbox in viewport coords.
            await page.evaluate(
                "(id)=>{window.scrollTo(0,0);document.getElementById(id).scrollIntoView({block:'start',behavior:'instant'});}",
                v,
            )
            await page.wait_for_timeout(500)
            box = await page.evaluate(
                "(id)=>{const el=document.getElementById(id);const r=el.getBoundingClientRect();return {x:r.left,y:r.top,w:r.width,h:r.height,sy:window.scrollY};}",
                v,
            )
            out = ROOT / f"shot-{v}-desktop.png"
            clip = {"x": box["x"], "y": box["y"], "width": box["w"], "height": box["h"]}
            try:
                await page.screenshot(path=str(out), clip=clip, timeout=20000)
                print(f"wrote {out.name}  scrollY={int(box['sy'])} clipY={int(box['y'])} h={int(box['h'])}")
            except Exception as e:
                print("skip", out.name, type(e).__name__, str(e)[:80])
        await browser.close()


if __name__ == "__main__":
    asyncio.run(render())
