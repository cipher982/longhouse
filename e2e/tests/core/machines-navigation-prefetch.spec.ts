import { expect, test } from "../fixtures";

test("preloads the Machines route on hover without fetching activity early", async ({ page }) => {
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
  await machinesTab.hover();
  await expect.poll(() => routeScriptFinished).toBe(true);
  await expect(page).toHaveURL(/\/timeline(?:\?.*)?$/);
  expect(summaryRequests).toBe(0);

  await machinesTab.click();
  await expect(page).toHaveURL(/\/machines(?:\?.*)?$/);
  await expect(page.getByRole("heading", { name: "Machines" })).toBeVisible();
  await expect.poll(() => summaryRequests).toBeGreaterThan(0);
});
