import { test, expect } from "../tests/fixtures";

const testCount = Number.parseInt(process.env.PROBE_TEST_COUNT ?? "64", 10);
const holdMs = Number.parseInt(process.env.PROBE_HOLD_MS ?? "250", 10);

// Module-scope state is per-Playwright-commis-process.
let firstFicheIdSeen: number | null = null;

test.describe("Backend Parallelism Probe", () => {
  test("probe config sanity", async ({ request }) => {
    expect(testCount).toBeGreaterThan(0);
    expect(holdMs).toBeGreaterThanOrEqual(0);
    const res = await request.get("/");
    expect(res.status()).toBe(200);
  });

  for (let i = 0; i < testCount; i++) {
    test(`create fiche ${i}`, async ({ request }, testInfo) => {
      const res = await request.post("/api/fiches", {
        data: {
          name: `Probe Fiche ${testInfo.commisIndex}-${i}`,
          system_instructions: "probe",
          task_instructions: "probe",
          model: "gpt-5-nano",
        },
      });
      expect(res.status()).toBe(201);
      const created = await res.json();
      if (firstFicheIdSeen === null) {
        firstFicheIdSeen = created.id;
        // With per-commis SQLite isolation, each Playwright commis's first created fiche should be ID=1.
        expect(created.id).toBe(1);
      }
      if (holdMs > 0) {
        await new Promise((resolve) => setTimeout(resolve, holdMs));
      }
    });
  }
});
