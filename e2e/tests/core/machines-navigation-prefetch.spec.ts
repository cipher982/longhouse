import { expect, test } from "../fixtures";
import type { Page } from "../fixtures";

async function assertMachinesPrefetch(page: Page, trigger: "hover" | "keyboard"): Promise<void> {
  await page.goto("/timeline");
  await expect(page.getByRole("heading", { name: "Connect your first machine" })).toBeVisible();

  const origin = new URL(page.url()).origin;
  const loadedScripts = new Set(
    await page.evaluate(() =>
      performance.getEntriesByType("resource")
        .filter((entry) => entry.initiatorType === "script")
        .map((entry) => entry.name),
    ),
  );
  let routeScriptFinished = false;
  let summaryRequests = 0;
  page.on("requestfinished", (request) => {
    const url = new URL(request.url());
    if (
      request.resourceType() === "script" &&
      url.origin === origin &&
      url.pathname.includes("MachinesPage") &&
      !loadedScripts.has(url.href)
    ) {
      routeScriptFinished = true;
    }
  });
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === "/api/timeline/machines/summary") summaryRequests++;
  });

  const machinesTab = page.getByTestId("global-machines-tab");
  if (trigger === "hover") {
    await machinesTab.hover();
  } else {
    await page.getByTestId("global-timeline-tab").focus();
    await page.keyboard.press("Tab");
    await expect(machinesTab).toBeFocused();
  }
  await expect.poll(() => routeScriptFinished, { timeout: 5000 }).toBe(true);
  await expect(page).toHaveURL(/\/timeline(?:\?.*)?$/);
  expect(summaryRequests).toBe(0);

  await machinesTab.click();
  await expect(page).toHaveURL(/\/machines(?:\?.*)?$/);
  await expect(page.getByRole("heading", { name: "Machines" })).toBeVisible();
  await expect.poll(() => summaryRequests).toBeGreaterThan(0);
}

test("preloads Machines code on pointer hover without fetching activity early", async ({ page }) => {
  await assertMachinesPrefetch(page, "hover");
});

test("preloads Machines code on keyboard focus without fetching activity early", async ({ page }) => {
  await assertMachinesPrefetch(page, "keyboard");
});
