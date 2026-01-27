import fs from "fs";
import path from "path";
import { test, expect } from "@playwright/test";

type ProbeEvent =
  | { type: "start"; name: string; commisIndex: number; t: number }
  | { type: "end"; name: string; commisIndex: number; t: number };

const probeDir = path.resolve(process.cwd(), "test-results", "parallelism-probe");

function writeEvent(commisIndex: number, event: ProbeEvent) {
  fs.mkdirSync(probeDir, { recursive: true });
  const file = path.join(probeDir, `commis-${commisIndex}.jsonl`);
  fs.appendFileSync(file, `${JSON.stringify(event)}\n`, "utf8");
}

const testCount = Number.parseInt(process.env.PROBE_TEST_COUNT ?? "64", 10);
const sleepMs = Number.parseInt(process.env.PROBE_SLEEP_MS ?? "2000", 10);

test.describe("Scheduler Parallelism Probe", () => {
  test("probe config sanity", async ({}, testInfo) => {
    expect(testCount).toBeGreaterThan(0);
    expect(sleepMs).toBeGreaterThan(0);
    writeEvent(testInfo.commisIndex, { type: "start", name: testInfo.title, commisIndex: testInfo.commisIndex, t: Date.now() });
    await new Promise(resolve => setTimeout(resolve, 25));
    writeEvent(testInfo.commisIndex, { type: "end", name: testInfo.title, commisIndex: testInfo.commisIndex, t: Date.now() });
  });

  for (let i = 0; i < testCount; i++) {
    test(`probe sleep ${i}`, async ({}, testInfo) => {
      writeEvent(testInfo.commisIndex, { type: "start", name: testInfo.title, commisIndex: testInfo.commisIndex, t: Date.now() });
      await new Promise(resolve => setTimeout(resolve, sleepMs));
      writeEvent(testInfo.commisIndex, { type: "end", name: testInfo.title, commisIndex: testInfo.commisIndex, t: Date.now() });
    });
  }
});
