import { test, expect } from "../fixtures";
import { resetDatabase } from "../test-utils";

const inboxItems = [
  {
    card_id: 42,
    session_id: "sess-1",
    title: "Candidate Interview Recaps and Hiring Pitches",
    project: "hiring",
    machine: "zerg-commis-cloud",
    provider: "claude",
    loop_mode: "assist",
    decision: "wait",
    execution_state: "awaiting_user_approval",
    summary: "Comparable global caste-like systems detailed; awaiting user direction back to hiring task or next query",
    recommended_action: "ask_user",
    follow_up_prompt: "Wait for a user decision before resuming the hiring workflow.",
    blocked_reasons: [],
    last_turn_at: "2026-03-21T15:13:00Z",
    card_state: "active",
    card_state_reason: null,
    superseded_by_card_id: null,
    requires_attention: true,
  },
  {
    card_id: 99,
    session_id: "sess-2",
    title: "Settings and Modal Ownership Committed",
    project: "zerg",
    machine: "shipper-laptop",
    provider: "codex",
    loop_mode: "assist",
    decision: "continue",
    execution_state: "awaiting_user_approval",
    summary: "AI inspecting/editing infra cleanup tranche (useWebSocket.tsx, debounce/callback-sync, legacy forum)",
    recommended_action: "continue_session",
    follow_up_prompt: "Continue the cleanup tranche and validate the forum state handoff.",
    blocked_reasons: [],
    last_turn_at: "2026-03-21T17:07:00Z",
    card_state: "active",
    card_state_reason: null,
    superseded_by_card_id: null,
    requires_attention: true,
  },
];

const cards = {
  42: {
    ...inboxItems[0],
    rationale:
      "The answer fully handled the off-topic question, but it no longer advances the hiring task without fresh user direction.",
    mode_capability: "notify_only",
    mode_summary: "Suggest or escalate from completed turns, but wait for approval before continuing.",
    last_user_text: "what other countries have a system like this?",
    last_assistant_text:
      "Comparable global caste-like systems detailed; awaiting user direction back to hiring task or next query.",
    available_actions: ["approve_recommended_action", "not_now"],
  },
  99: {
    ...inboxItems[1],
    rationale:
      "The infrastructure cleanup is bounded, the tranche is clearly scoped, and the agent already identified the remaining verification.",
    mode_capability: "continue",
    mode_summary: "One bounded follow-up is allowed when the review explicitly permits continuation.",
    last_user_text: "clean up the remaining modal ownership bugs",
    last_assistant_text:
      "I finished the settings/modal ownership pass and only the final cleanup tranche remains.",
    available_actions: ["approve_recommended_action", "not_now"],
  },
  390: {
    ...inboxItems[1],
    card_id: 390,
    session_id: "sess-stale",
    title: "Frontend Effect Cleanup Fully Completed",
    summary: "This older card is no longer the active thing to review.",
    rationale:
      "A newer turn superseded this review, so the remaining useful action is to open the active follow-up instead.",
    card_state: "superseded",
    card_state_reason: "A newer turn replaced this follow-up.",
    superseded_by_card_id: 99,
    available_actions: [],
  },
};

async function mockLoopInboxApi(page: import("@playwright/test").Page): Promise<void> {
  await page.route("**/api/oikos/loop-inbox**", async (route) => {
    const url = new URL(route.request().url());
    const pathname = url.pathname;

    if (pathname.endsWith("/api/oikos/loop-inbox")) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(inboxItems),
      });
      return;
    }

    const cardMatch = pathname.match(/\/api\/oikos\/loop-inbox\/cards\/(\d+)$/);
    if (cardMatch) {
      const cardId = Number(cardMatch[1]);
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(cards[cardId as 42 | 99]),
      });
      return;
    }

    await route.continue();
  });
}

async function saveScreenshot(
  page: import("@playwright/test").Page,
  testInfo: import("@playwright/test").TestInfo,
  name: string,
): Promise<void> {
  const path = testInfo.outputPath(name);
  await page.screenshot({ path, fullPage: false });
  await testInfo.attach(name, { path, contentType: "image/png" });
}

test.beforeEach(async ({ request }) => {
  await resetDatabase(request);
});

test("loop inbox keeps the card primary and opens the queue as a left drawer on mobile", async ({
  page,
}, testInfo) => {
  await mockLoopInboxApi(page);

  await page.goto("/loop/card/42");

  const card = page.getByTestId("loop-inbox-card");
  const header = page.getByTestId("loop-mobile-header");
  const queueButton = page.getByTestId("loop-mobile-queue-button");
  const queueCount = page.getByTestId("loop-mobile-queue-count");

  await expect(card).toBeVisible();
  await expect(header).toBeVisible();
  await expect(queueButton).toBeVisible();
  await expect(queueButton).toHaveAttribute("aria-label", /Open follow-ups/);
  await expect(queueCount).toHaveText("2");
  await expect(page.getByRole("link", { name: "Open timeline" })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Candidate Interview Recaps and Hiring Pitches" })).toBeVisible();
  await expect(page.getByText(/^Attention queue$/)).toHaveCount(0);
  await expect(page.getByText(/^2 open follow-ups$/)).toHaveCount(0);

  const viewport = page.viewportSize();
  if (!viewport) {
    throw new Error("Mobile viewport was not configured for the test.");
  }

  const cardBox = await card.boundingBox();
  expect(cardBox).toBeTruthy();
  expect(cardBox?.y ?? Number.POSITIVE_INFINITY).toBeLessThan(viewport.height * 0.24);

  const headerBox = await header.boundingBox();
  const queueButtonBox = await queueButton.boundingBox();
  expect(headerBox).toBeTruthy();
  expect(queueButtonBox).toBeTruthy();
  expect(queueButtonBox?.width ?? 0).toBeGreaterThanOrEqual(44);
  expect(queueButtonBox?.height ?? 0).toBeGreaterThanOrEqual(44);
  expect(queueButtonBox?.y ?? Number.POSITIVE_INFINITY).toBeLessThan(cardBox?.y ?? 0);
  expect((headerBox?.y ?? 0) + (headerBox?.height ?? 0)).toBeLessThanOrEqual((cardBox?.y ?? 0) + 2);
  await saveScreenshot(page, testInfo, "loop-inbox-mobile-closed.png");

  await queueButton.click();

  const drawer = page.getByTestId("loop-mobile-queue-drawer");
  await expect(drawer).toBeVisible();
  await expect(page.getByTestId("loop-mobile-queue-scrim")).toBeVisible();
  await expect(drawer.getByRole("heading", { name: "Follow-ups" })).toBeVisible();
  await expect(drawer.getByRole("link", { name: "Open timeline" })).toBeVisible();
  await expect(drawer.getByText("Settings and Modal Ownership Committed")).toBeVisible();

  const drawerBox = await drawer.boundingBox();
  expect(drawerBox).toBeTruthy();
  expect(drawerBox?.x ?? Number.POSITIVE_INFINITY).toBeLessThanOrEqual(1);
  expect(drawerBox?.width ?? 0).toBeLessThan(viewport.width * 0.9);
  await saveScreenshot(page, testInfo, "loop-inbox-mobile-drawer-open.png");

  await drawer.getByTestId("loop-inbox-row-99").click();

  await page.waitForURL("**/loop/card/99", { timeout: 10000 });
  await expect(page.getByTestId("loop-mobile-queue-drawer")).toHaveCount(0);
  await expect(page.getByTestId("loop-mobile-header")).toBeVisible();
  await expect(page.getByTestId("loop-mobile-queue-button")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Settings and Modal Ownership Committed" })).toBeVisible();
});

test("loop inbox auto-opens the queue when a stale mobile card is selected", async ({
  page,
}) => {
  await mockLoopInboxApi(page);

  await page.goto("/loop/card/390");

  const drawer = page.getByTestId("loop-mobile-queue-drawer");
  const statusBanner = page.getByTestId("loop-inbox-card-status-banner");

  await expect(page.getByTestId("loop-mobile-header")).toBeVisible();
  await expect(page.getByTestId("loop-mobile-queue-button")).toBeVisible();
  await expect(page.getByTestId("loop-mobile-queue-count")).toHaveText("2");
  await expect(drawer).toBeVisible();
  await expect(drawer.getByText("Settings and Modal Ownership Committed")).toBeVisible();
  await expect(statusBanner).toContainText("Viewing older card");
  await expect(statusBanner).toContainText("A newer turn replaced this follow-up.");
  await expect(statusBanner.getByRole("link", { name: "Open current" })).toHaveAttribute("href", "/loop/card/99");
});
