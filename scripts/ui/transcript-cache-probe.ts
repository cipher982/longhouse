/**
 * Does a reload paint the session from disk before the network answers?
 * (session-view-terminal-parity C5)
 *
 * Serves the session-prose-idle fixture, opens the session once so the
 * transcript lands in IndexedDB, then reloads it twice with the transcript
 * endpoints held back for HOLD_MS: once in that same browser profile (warm) and
 * once in a fresh one (cold). Prints time to the first transcript row for each
 * and saves a frame taken while the network was still held.
 *
 *   bunx tsx scripts/ui/transcript-cache-probe.ts [--hold=3000] [--output=/tmp/agents/transcript-cache-probe]
 *   FRONTEND_URL=http://localhost:47231 (default) picks the Vite port.
 */
import { chromium, type BrowserContext } from "playwright";
import { mkdirSync } from "fs";
import path from "path";
import { ensureFrontend } from "./frontend";
import { PAGE_DEFINITIONS, installSceneMocks } from "./ui-capture";

const arg = (name: string, fallback: string) =>
  process.argv.find((value) => value.startsWith(`--${name}=`))?.split("=")[1] ?? fallback;
const HOLD_MS = Number(arg("hold", "3000"));
const OUTPUT = arg("output", "/tmp/agents/transcript-cache-probe");
const BASE_URL = process.env.FRONTEND_URL ?? "http://localhost:47231";
const ROW = ".timeline-pane__rows [id]";
const TRANSCRIPT_PATH = /\/sessions\/[^/]+\/(workspace|projection)$/;

async function holdTranscript(context: BrowserContext): Promise<void> {
  // Routes registered later run first; fall back to the scene mocks after the hold.
  await context.route(`${new URL(BASE_URL).origin}/api/**`, async (route) => {
    if (TRANSCRIPT_PATH.test(new URL(route.request().url()).pathname)) {
      await new Promise((resolve) => setTimeout(resolve, HOLD_MS));
    }
    await route.fallback();
  });
}

async function timeToFirstRow(context: BrowserContext, frame: string): Promise<number> {
  const page = await context.newPage();
  try {
    const url = `${BASE_URL}${PAGE_DEFINITIONS["session-detail"].path}`;
    const started = Date.now();
    await page.goto(url);
    await page.waitForSelector(ROW, { timeout: HOLD_MS + 15_000 });
    const elapsed = Date.now() - started;
    await page.screenshot({ path: path.join(OUTPUT, `${frame}.png`) });
    return elapsed;
  } finally {
    await page.close();
  }
}

async function main() {
  mkdirSync(OUTPUT, { recursive: true });
  const stopFrontend = await ensureFrontend(BASE_URL);
  const browser = await chromium.launch();
  try {
    const viewport = { width: 1440, height: 900 };
    const warm = await browser.newContext({ viewport });
    await installSceneMocks(warm, "session-prose-idle", BASE_URL);
    // First visit: network as usual; give the 1 s write delay time to land.
    const first = await warm.newPage();
    await first.goto(`${BASE_URL}${PAGE_DEFINITIONS["session-detail"].path}`);
    await first.waitForSelector(ROW, { timeout: 15_000 });
    await first.waitForTimeout(2_500);
    await first.close();

    await holdTranscript(warm);
    const warmMs = await timeToFirstRow(warm, "warm-reload-network-held");

    const cold = await browser.newContext({ viewport });
    await installSceneMocks(cold, "session-prose-idle", BASE_URL);
    await holdTranscript(cold);
    const coldMs = await timeToFirstRow(cold, "cold-load-network-held");

    console.log(`transcript held back ${HOLD_MS} ms`);
    console.log(`cold load: first transcript row after ${coldMs} ms`);
    console.log(`warm reload: first transcript row after ${warmMs} ms`);
    console.log(`frames: ${OUTPUT}`);
    if (warmMs >= HOLD_MS) process.exitCode = 1;
  } finally {
    await browser.close();
    await stopFrontend();
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
