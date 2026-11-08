#!/usr/bin/env bun
/**
 * iOS transcript stability check (L1, specs/ios-transcript-stability.md).
 *
 * Plays content changes against the shipped transcript document in Playwright
 * WebKit at phone size and fails when the row being read moves, the pinned
 * bottom drifts, or UI state is lost.
 *
 *   bunx tsx scripts/qa/ios-transcript-stability.ts [--only=name,name] [--out=dir] [--dark]
 *
 * Reads ios/Resources/Transcript/transcript.html: run `make generate-ios-transcript`
 * first (the make target does).
 */
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { Harness } from "./ios-transcript-stability/lib.ts";
import { scenarios, type Check } from "./ios-transcript-stability/scenarios.ts";

const arg = (name: string) => process.argv.find((a) => a.startsWith(`--${name}=`))?.split("=")[1];
const only = arg("only")?.split(",");
const stamp = new Date().toISOString().replace(/[-:]/g, "").slice(0, 15);
const out = arg("out") ?? path.join("/tmp/agents/ios-transcript-stability", stamp);
mkdirSync(out, { recursive: true });

const browser = await Harness.launch();
const results: { name: string; about: string; checks: Check[]; error?: string }[] = [];
try {
  for (const scenario of scenarios) {
    if (only && !only.includes(scenario.name)) continue;
    const h = new Harness();
    await h.open(browser, { dark: process.argv.includes("--dark") });
    try {
      const checks = await scenario.run(h);
      await h.screenshot(path.join(out, `${scenario.name}.png`));
      results.push({ name: scenario.name, about: scenario.about, checks });
    } catch (error) {
      results.push({ name: scenario.name, about: scenario.about, checks: [], error: String((error as Error).stack ?? error) });
    } finally {
      await h.close();
    }
  }
} finally {
  await browser.close();
}

let failed = 0;
for (const r of results) {
  if (r.error) {
    failed++;
    console.log(`ERROR ${r.name}\n  ${r.error.split("\n").slice(0, 4).join("\n  ")}`);
    continue;
  }
  for (const c of r.checks) {
    const status = c.pass ? "ok  " : c.advisory ? "warn" : "FAIL";
    if (!c.pass && !c.advisory) failed++;
    console.log(`${status} ${r.name.padEnd(28)} ${c.name.padEnd(40)} ${String(c.value).padStart(8)} ${c.unit} (budget ${c.budget})`);
  }
}
writeFileSync(path.join(out, "results.json"), JSON.stringify({ results, failed }, null, 2));
console.log(`\n${failed ? `${failed} failing` : "all checks pass"}; evidence in ${out}`);
process.exit(failed ? 1 : 0);
