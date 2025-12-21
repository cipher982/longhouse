import fs from "fs";
import path from "path";
import { test, expect } from "@playwright/test";

type ProbeEvent =
  | { type: "start"; name: string; workerIndex: number; t: number }
  | { type: "end"; name: string; workerIndex: number; t: number };

const probeDir = path.resolve(process.cwd(), "test-results", "parallelism-probe");

function writeEvent(workerIndex: number, event: ProbeEvent) {
  fs.mkdirSync(probeDir, { recursive: true });
  const file = path.join(probeDir, `worker-${workerIndex}.jsonl`);
  fs.appendFileSync(file, `${JSON.stringify(event)}\n`, "utf8");
}

const testCount = Number.parseInt(process.env.PROBE_TEST_COUNT ?? "64", 10);
const sleepMs = Number.parseInt(process.env.PROBE_SLEEP_MS ?? "2000", 10);

test.describe("Scheduler Parallelism Probe", () => {
  test("probe config sanity", async ({}, testInfo) => {
    expect(testCount).toBeGreaterThan(0);
    expect(sleepMs).toBeGreaterThan(0);
    writeEvent(testInfo.workerIndex, { type: "start", name: testInfo.title, workerIndex: testInfo.workerIndex, t: Date.now() });
    await new Promise(resolve => setTimeout(resolve, 25));
    writeEvent(testInfo.workerIndex, { type: "end", name: testInfo.title, workerIndex: testInfo.workerIndex, t: Date.now() });
  });

  for (let i = 0; i < testCount; i++) {
    test(`probe sleep ${i}`, async ({}, testInfo) => {
      writeEvent(testInfo.workerIndex, { type: "start", name: testInfo.title, workerIndex: testInfo.workerIndex, t: Date.now() });
      await new Promise(resolve => setTimeout(resolve, sleepMs));
      writeEvent(testInfo.workerIndex, { type: "end", name: testInfo.title, workerIndex: testInfo.workerIndex, t: Date.now() });
    });
  }
});
