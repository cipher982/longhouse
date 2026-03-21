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

  const header = page.getByTestId("loop-mobile-header");
  const card = page.getByTestId("loop-inbox-card");
  const queueToggle = page.getByTestId("loop-mobile-queue-toggle");

  await expect(header).toBeVisible();
  await expect(card).toBeVisible();
  await expect(queueToggle).toBeVisible();
  await expect(header).toContainText("Loop Inbox");
  await expect(header).toContainText("Viewing 1 of 2");
  await expect(queueToggle).toContainText("Follow-ups");
  await expect(page.getByRole("link", { name: "Open timeline" })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Candidate Interview Recaps and Hiring Pitches" })).toBeVisible();
  await expect(page.getByText(/^Attention queue$/)).toHaveCount(0);

  const viewport = page.viewportSize();
  if (!viewport) {
    throw new Error("Mobile viewport was not configured for the test.");
  }

  const cardBox = await card.boundingBox();
  expect(cardBox).toBeTruthy();
  expect(cardBox?.y ?? Number.POSITIVE_INFINITY).toBeLessThan(viewport.height * 0.23);
  await saveScreenshot(page, testInfo, "loop-inbox-mobile-closed.png");

  await queueToggle.click();

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
  await expect(page.getByText("Viewing 2 of 2")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Settings and Modal Ownership Committed" })).toBeVisible();
});
