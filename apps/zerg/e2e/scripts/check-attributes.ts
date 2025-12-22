#!/usr/bin/env bun
/**
 * Check data attributes on the page
 */

import { chromium } from "playwright";

async function main() {
  const url = process.argv[2] || "http://localhost:30080/?fx=none";

  console.log(`\nChecking attributes on: ${url}\n`);

  const browser = await chromium.launch({ headless: false });
  const page = await browser.newPage();
  await page.goto(url, { waitUntil: "networkidle" });
  await page.waitForTimeout(2000);

  const attrs = await page.evaluate(() => {
    const root = document.getElementById("react-root");
    const landingPage = document.querySelector(".landing-page");

    return {
      reactRoot: {
        dataUiEffects: root?.getAttribute("data-ui-effects"),
        exists: !!root,
      },
      landingPage: {
        dataFxHero: landingPage?.getAttribute("data-fx-hero"),
        dataFxParticles: landingPage?.getAttribute("data-fx-particles"),
        exists: !!landingPage,
      },
    };
  });

  console.log("Attributes found:");
  console.log(JSON.stringify(attrs, null, 2));

  await browser.close();
}

main().catch(console.error);
