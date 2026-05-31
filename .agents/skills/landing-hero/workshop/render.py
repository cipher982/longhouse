#!/usr/bin/env python3
"""Render each variant by bounding-box clip."""
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

ROOT = Path(__file__).parent
URL = f"file://{ROOT}/variants.html"
# Finalists (by variants.html badge label): A/F/G/J/K.
# The section ids are inconsistent with label letters (historical drift), so we
# pick each section by the letter in its `.variant-label` text and write the
# shot under the badge-letter filename. This keeps filenames and content
# aligned even if section ids change.
VARIANT_LABELS = ["A", "F", "G", "J", "K"]
# Pin chromium to avoid playwright-version-vs-cached-chromium drift. If this
# path doesn't exist on your machine, run:
#   uv run --with "playwright==1.50.0" playwright install chromium
# then point EXEC at one of ~/Library/Caches/ms-playwright/chromium-*/chrome-mac-arm64/Google Chrome for Testing.app/...
EXEC = str(
    Path.home()
    / "Library/Caches/ms-playwright/chromium-1200/chrome-mac-arm64"
    / "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
)


SLUGS = {
    "A": "timeline-primary",
    "F": "big-phone",
    "G": "terminal-timeline-phone",
    "J": "scattered-consolidated",
    "K": "max-overlap",
}


async def render():
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=EXEC)
        ctx = await browser.new_context(viewport={"width": 1440, "height": 1500}, device_scale_factor=2)
        page = await ctx.new_page()
        await page.goto(URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)
        # Map each badge letter to the section id whose .variant-label starts with that letter.
        label_to_id = await page.evaluate(
            """() => {
                const out = {};
                document.querySelectorAll('section.variant').forEach(sec => {
                    const lbl = sec.querySelector('.variant-label');
                    if (!lbl) return;
                    const m = lbl.textContent.trim().match(/^([A-Z0-9]+)/);
                    if (m) out[m[1]] = sec.id;
                });
                return out;
            }"""
        )
        for letter in VARIANT_LABELS:
            sec_id = label_to_id.get(letter)
            if not sec_id:
                print(f"skip {letter}: no section with that label")
                continue
            slug = SLUGS.get(letter, "variant")
            out = ROOT / f"shot-{letter}-{slug}.png"
            try:
                el = await page.query_selector(f"#{sec_id}")
                if not el:
                    print(f"skip {out.name}: element #{sec_id} not found")
                    continue
                await el.scroll_into_view_if_needed()
                await page.wait_for_timeout(400)
                await el.screenshot(
                    path=str(out),
                    timeout=60000,
                    animations="disabled",
                )
                print(f"wrote {out.name} (section #{sec_id})")
            except Exception as e:
                print("skip", out.name, type(e).__name__, str(e)[:80])
        await browser.close()


if __name__ == "__main__":
    asyncio.run(render())
