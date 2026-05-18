/**
 * Standalone browser verification for the installer first-run flow.
 *
 * Connects to an already-running Longhouse server (started by installer-first-run.sh)
 * and validates that demo sessions are visible in the timeline.
 *
 * Usage:
 *   bunx tsx e2e/scripts/verify-installer-browser.ts --url http://127.0.0.1:PORT
 */
import { chromium } from "playwright";

function parseArgs(): { url: string } {
  const args = process.argv.slice(2);
  const urlIdx = args.indexOf("--url");
  const url = urlIdx >= 0 ? args[urlIdx + 1] : undefined;
  if (!url) {
    console.error("Usage: verify-installer-browser.ts --url <server-url>");
    process.exit(1);
  }
  return { url };
}

async function main() {
  const { url } = parseArgs();
  const timelineUrl = `${url}/timeline`;

  console.log(`Connecting to ${timelineUrl}...`);

  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();

  try {
    await page.goto(timelineUrl, { waitUntil: "domcontentloaded", timeout: 30_000 });

    // Wait for the timeline to reach its ready state
    await page.waitForSelector('[data-ready="true"]', { timeout: 20_000 });

    // Count session rows — demo-fresh seeds ~10 sessions
    const rowCount = await page.getByTestId("session-row").count();
    if (rowCount === 0) {
      throw new Error("No session rows found in timeline after demo seed");
    }

    // Click the first session row and verify detail page loads
    await page.getByTestId("session-row").first().click();
    await page.waitForSelector('[data-ready="true"]', { timeout: 15_000 });
    const detailUrl = page.url();
    if (!detailUrl.includes("/timeline/")) {
      throw new Error(`Expected session detail URL, got: ${detailUrl}`);
    }

    console.log(`Timeline: ${rowCount} sessions visible`);
    console.log(`Session detail loaded: ${detailUrl}`);
    console.log("Browser verification passed");
  } catch (err) {
    // Capture screenshot on failure for CI debugging
    const screenshotPath = "/tmp/installer-browser-verify-failure.png";
    await page.screenshot({ path: screenshotPath, fullPage: true }).catch(() => {});
    console.error(`Screenshot saved: ${screenshotPath}`);
    throw err;
  } finally {
    await browser.close();
  }
}

main().catch((err) => {
  console.error("Browser verification failed:", err instanceof Error ? err.message : err);
  process.exit(1);
});
