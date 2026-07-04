import { test, expect } from "../tests/fixtures";

const testCount = Number.parseInt(process.env.PROBE_TEST_COUNT ?? "64", 10);
const holdMs = Number.parseInt(process.env.PROBE_HOLD_MS ?? "250", 10);

// Module-scope state is per Playwright worker process.
let firstAutomationIdSeen: number | null = null;

test.describe("Backend Parallelism Probe", () => {
  test("probe config sanity", async ({ request }) => {
    expect(testCount).toBeGreaterThan(0);
    expect(holdMs).toBeGreaterThanOrEqual(0);
    const res = await request.get("/");
    expect(res.status()).toBe(200);
  });

  for (let i = 0; i < testCount; i++) {
    test(`create automation ${i}`, async ({ request }, testInfo) => {
      const res = await request.post("/api/automations", {
        data: {
          name: `Probe Automation ${testInfo.parallelIndex}-${i}`,
          system_instructions: "probe",
          task_instructions: "probe",
          model: "deepseek/deepseek-v4-flash",
        },
      });
      expect(res.status()).toBe(201);
      const created = await res.json();
      if (firstAutomationIdSeen === null) {
        firstAutomationIdSeen = created.id;
        // With per-worker SQLite isolation, each Playwright worker's first created automation should be ID=1.
        expect(created.id).toBe(1);
      }
      if (holdMs > 0) {
        await new Promise((resolve) => setTimeout(resolve, holdMs));
      }
    });
  }
});
