#!/usr/bin/env bun
/**
 * Find which DOM elements have running CSS animations
 */

import { chromium } from "playwright";

async function main() {
  const url = process.argv[2] || "http://localhost:30080/?fx=none";

  console.log(`\nInspecting animations on: ${url}\n`);

  const browser = await chromium.launch({ headless: false });
  const page = await browser.newPage();
  await page.goto(url, { waitUntil: "networkidle" });
  await page.waitForTimeout(2000);

  // Get all elements with active animations
  const animatedElements = await page.evaluate(() => {
    const allElements = document.querySelectorAll("*");
    const result: Array<{ selector: string; animations: string[] }> = [];

    for (const el of allElements) {
      const computed = window.getComputedStyle(el);
      const animationName = computed.animationName;

      if (animationName && animationName !== "none") {
        const animationDuration = computed.animationDuration;
        const animationIterationCount = computed.animationIterationCount;

        // Try to build a useful selector
        let selector = el.tagName.toLowerCase();
        if (el.id) selector += `#${el.id}`;
        if (el.className && typeof el.className === "string") {
          const classes = el.className.split(" ").filter(Boolean).slice(0, 3);
          if (classes.length) selector += `.${classes.join(".")}`;
        }

        result.push({
          selector,
          animations: [
            `${animationName} (duration: ${animationDuration}, iterations: ${animationIterationCount})`,
          ],
        });
      }
    }

    return result;
  });

  console.log(`Found ${animatedElements.length} elements with animations:\n`);

  for (const item of animatedElements) {
    console.log(`  ${item.selector}`);
    for (const anim of item.animations) {
      console.log(`    - ${anim}`);
    }
  }

  await browser.close();
}

main().catch(console.error);
