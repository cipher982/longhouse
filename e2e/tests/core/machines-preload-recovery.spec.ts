import { test, expect } from "../fixtures";
import { resetDatabase } from "../test-utils";

test.beforeEach(async ({ request }) => {
  await resetDatabase(request);
});

test("shows a reload action and recovers after a failed Machines chunk", async ({ page }) => {
  await page.goto("/timeline");
  const machinesTab = page.getByRole("button", { name: "Machines", exact: true });
  await expect(machinesTab).toBeVisible();

  let failFirstMachinesChunk = true;
  let machinesChunkRequests = 0;
  await page.route("**/assets/MachinesPage-*.js", async (route) => {
    machinesChunkRequests += 1;
    if (failFirstMachinesChunk) {
      failFirstMachinesChunk = false;
      await route.abort();
      return;
    }
    await route.continue();
  });

  await machinesTab.click();
  await expect(page.getByText("Longhouse could not load this page. Retry the page load.")).toBeVisible({
    timeout: 20_000,
  });
  expect(await page.evaluate(() => performance.getEntriesByType("navigation")[0]?.type)).toBe("navigate");
  expect(failFirstMachinesChunk).toBe(false);

  await page.getByRole("button", { name: "Reload to try again" }).click();
  await expect(page.getByRole("heading", { name: "Machines", exact: true })).toBeVisible({ timeout: 30_000 });
  expect(await page.evaluate(() => performance.getEntriesByType("navigation")[0]?.type)).toBe("reload");
  expect(machinesChunkRequests).toBeGreaterThanOrEqual(2);
});
