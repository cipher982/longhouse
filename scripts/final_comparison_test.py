#!/usr/bin/env python3
"""Final comparison test - none vs high reasoning effort."""

import asyncio
from playwright.async_api import async_playwright

async def test():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()

        await page.goto("http://localhost:30080/chat")
        await page.wait_for_timeout(2000)

        # Test 1: none
        print("="*60)
        print("TEST 1: reasoning_effort = none")
        print("="*60)

        selector = page.locator('.reasoning-select')
        await selector.select_option('none')
        print("✅ Set to 'none'")

        input_box = page.locator('.text-input')
        await input_box.fill("Say hello in 3 words")
        await page.locator('.send-button').click()
        await page.wait_for_timeout(5000)

        badge_count_1 = await page.locator('.debug-badge').count()
        print(f"Badge count: {badge_count_1}")

        # Test 2: high
        print("\n" + "="*60)
        print("TEST 2: reasoning_effort = high")
        print("="*60)

        await selector.select_option('high')
        print("✅ Set to 'high'")

        await input_box.fill("Calculate 47 * 89 step by step")
        await page.locator('.send-button').click()
        await page.wait_for_timeout(6000)

        badge_count_2 = await page.locator('.debug-badge').count()
        print(f"Badge count: {badge_count_2}")

        if badge_count_2 > 0:
            badge = page.locator('.debug-badge').last
            text = await badge.text_content()
            print(f"✅ Badge text: {text}")

        # Final screenshot
        await page.screenshot(path='/Users/davidrose/git/zerg/final-comparison.png', full_page=True)
        print("\n📸 Screenshot saved: final-comparison.png")

        await page.wait_for_timeout(2000)
        await browser.close()

        print("\n" + "="*60)
        print("RESULTS:")
        print("="*60)
        print(f"  reasoning_effort='none': {badge_count_1} badges (expected: 0)")
        print(f"  reasoning_effort='high': {badge_count_2} badges (expected: ≥1)")

        if badge_count_1 == 0 and badge_count_2 > 0:
            print("\n✅ SUCCESS: Feature working correctly!")
        else:
            print("\n⚠️ Unexpected results")

if __name__ == "__main__":
    asyncio.run(test())
