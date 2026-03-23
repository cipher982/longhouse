import { test, expect } from "../fixtures";
import { resetDatabase } from "../test-utils";

// NOTE:
// These mobile Loop tests intentionally keep the browser/UI real while mocking the
// Loop backend APIs and device/runtime edges (web push delivery, service worker
// notification clicks, tmux-backed managed-local Claude sessions). Those pieces
// need separate integration/device canaries later; this file is the first
// pragmatic E2E slice for the mobile action UX and request contracts.

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

const managedLocalInboxItem = {
  card_id: 261,
  session_id: "sess-managed-local",
  title: "Managed Local Hiring Follow-up",
  project: "hiring",
  machine: "cinder",
  provider: "claude",
  execution_home: "managed_local",
  home_label: "On this Mac",
  loop_mode: "assist",
  decision: "continue",
  execution_state: "awaiting_user_approval",
  summary: "Claude paused after step 1 and is waiting to continue the exact Mac session.",
  recommended_action: "continue_session",
  follow_up_prompt: "Continue to step 2 in the same managed local session.",
  blocked_reasons: [],
  last_turn_at: "2026-03-22T14:10:00Z",
  card_state: "active",
  card_state_reason: null,
  superseded_by_card_id: null,
  requires_attention: true,
};

const managedLocalCard = {
  ...managedLocalInboxItem,
  rationale: "This session is managed on the source Mac, so Loop can safely continue or reply without cloud takeover.",
  mode_capability: "notify_only",
  mode_summary: "Suggest or escalate from completed turns, but wait for approval before continuing.",
  last_user_text: "Do step 1, then stop and ask me before step 2.",
  last_assistant_text: "Step 1 is complete. I am waiting for your go-ahead before step 2.",
  available_actions: ["approve_recommended_action", "reply_to_session", "not_now"],
};

async function mockLoopInboxApi(
  page: import("@playwright/test").Page,
  {
    items = inboxItems,
    cardMap = cards,
    onAction,
  }: {
    items?: typeof inboxItems;
    cardMap?: Record<number, unknown>;
    onAction?: (payload: {
      cardId: number;
      action: string | null;
      replyText: string | null;
    }) => Promise<{
      status?: number;
      body?: unknown;
      nextItems?: typeof inboxItems;
      nextCards?: Record<number, unknown>;
    } | void> | {
      status?: number;
      body?: unknown;
      nextItems?: typeof inboxItems;
      nextCards?: Record<number, unknown>;
    } | void;
  } = {},
): Promise<void> {
  let currentItems = items;
  let currentCardMap = cardMap;

  const handleLoopInboxRoute = async (route: import("@playwright/test").Route): Promise<void> => {
    const url = new URL(route.request().url());
    const pathname = url.pathname;

    if (pathname.endsWith("/api/oikos/loop-inbox")) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(currentItems),
      });
      return;
    }

    const cardMatch = pathname.match(/\/api\/oikos\/loop-inbox\/cards\/(\d+)$/);
    if (cardMatch) {
      const cardId = Number(cardMatch[1]);
      const card = currentCardMap[cardId];
      if (!card) {
        await route.fulfill({
          status: 404,
          contentType: "application/json",
          body: JSON.stringify({ detail: "Not found" }),
        });
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(card),
      });
      return;
    }

    const actionMatch = pathname.match(/\/api\/oikos\/loop-inbox\/cards\/(\d+)\/actions$/);
    if (actionMatch && route.request().method() === "POST") {
      const cardId = Number(actionMatch[1]);
      const payload = route.request().postDataJSON() as { action?: string; reply_text?: string | null };
      const outcome = (await onAction?.({
        cardId,
        action: typeof payload?.action === "string" ? payload.action : null,
        replyText: typeof payload?.reply_text === "string" ? payload.reply_text : null,
      })) ?? {};

      if (outcome.nextItems) {
        currentItems = outcome.nextItems;
      }
      if (outcome.nextCards) {
        currentCardMap = outcome.nextCards;
      }

      await route.fulfill({
        status: outcome.status ?? 200,
        contentType: "application/json",
        body: JSON.stringify(
          outcome.body ?? {
            session_id: `sess-${cardId}`,
            review_id: cardId,
            action: payload?.action ?? null,
            status: "acted",
            reason: null,
            queued_job_id: null,
          },
        ),
      });
      return;
    }

    await route.continue();
  };

  await page.route("**/api/oikos/loop-inbox", handleLoopInboxRoute);
  await page.route("**/api/oikos/loop-inbox/**", handleLoopInboxRoute);
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
  await expect(queueButton).toContainText("Follow-ups");
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
  // Keep this generous enough for the compact mobile chrome to evolve without
  // making the card disappear below the fold again.
  expect(cardBox?.y ?? Number.POSITIVE_INFINITY).toBeLessThan(viewport.height * 0.26);

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
  expect(drawerBox?.width ?? 0).toBeLessThan(viewport.width * 0.96);
  await saveScreenshot(page, testInfo, "loop-inbox-mobile-drawer-open.png");

  await drawer.getByTestId("loop-inbox-row-99").click();

  await page.waitForURL("**/loop/card/99", { timeout: 10000 });
  await expect(page.getByTestId("loop-mobile-queue-drawer")).not.toBeVisible();
  await expect(page.getByTestId("loop-mobile-queue-scrim")).not.toBeVisible();
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
  await expect(page.getByTestId("loop-mobile-queue-button")).toContainText("Follow-ups");
  await expect(page.getByTestId("loop-mobile-queue-count")).toHaveText("2");
  await expect(drawer).toBeVisible();
  await expect(drawer.getByText("Settings and Modal Ownership Committed")).toBeVisible();
  await expect(statusBanner).toContainText("Viewing older card");
  await expect(statusBanner).toContainText("A newer turn replaced this follow-up.");
  await expect(statusBanner.getByRole("link", { name: "Open current" })).toHaveAttribute("href", "/loop/card/99");
});

test("loop inbox keeps the mobile queue button visible when only one follow-up exists", async ({
  page,
}) => {
  await mockLoopInboxApi(page, {
    items: [inboxItems[0]],
    cardMap: { 42: cards[42] },
  });

  await page.goto("/loop/card/42");

  await expect(page.getByTestId("loop-mobile-header")).toBeVisible();
  await expect(page.getByTestId("loop-mobile-queue-button")).toBeVisible();
  await expect(page.getByTestId("loop-mobile-queue-button")).toContainText("Follow-ups");
  await expect(page.getByTestId("loop-mobile-queue-count")).toHaveText("1");

  await page.getByTestId("loop-mobile-queue-button").click();

  const drawer = page.getByTestId("loop-mobile-queue-drawer");
  await expect(drawer).toBeVisible();
  await expect(drawer.getByTestId("loop-inbox-row-42")).toBeVisible();
  await expect(drawer.getByText("No follow-ups right now")).toHaveCount(0);
});

test("loop inbox shows an explicit empty state in the mobile drawer when nothing is waiting", async ({
  page,
}) => {
  await mockLoopInboxApi(page, {
    items: [],
    cardMap: {},
  });

  await page.goto("/loop");

  await expect(page.getByTestId("loop-mobile-header")).toBeVisible();
  await expect(page.getByTestId("loop-mobile-queue-button")).toBeVisible();
  await expect(page.getByTestId("loop-mobile-queue-button")).toContainText("Follow-ups");
  await expect(page.getByTestId("loop-mobile-queue-count")).toHaveText("0");
  await expect(page.getByText("No sessions need attention")).toBeVisible();

  await page.getByTestId("loop-mobile-queue-button").click();

  const drawer = page.getByTestId("loop-mobile-queue-drawer");
  await expect(drawer).toBeVisible();
  await expect(drawer.getByTestId("loop-mobile-queue-drawer-empty")).toContainText("No follow-ups right now");
  await expect(drawer.getByText("New approvals will appear here as soon as a coding turn needs review.")).toBeVisible();
});

test("loop inbox mobile managed-local continue uses the exact card action contract (mocked transport boundary)", async ({
  page,
}) => {
  const actionCalls: Array<{ cardId: number; action: string | null; replyText: string | null }> = [];

  await mockLoopInboxApi(page, {
    items: [managedLocalInboxItem],
    cardMap: { 261: managedLocalCard },
    onAction: async (payload) => {
      actionCalls.push(payload);
      return {
        body: {
          session_id: managedLocalInboxItem.session_id,
          review_id: managedLocalInboxItem.card_id,
          action: payload.action,
          status: "acted",
          reason: null,
          queued_job_id: null,
        },
        nextItems: [],
        nextCards: {
          261: {
            ...managedLocalCard,
            card_state: "acted",
            card_state_reason: "Continue was sent to the managed local session.",
          },
        },
      };
    },
  });

  await page.goto("/loop/card/261");

  await expect(page.getByTestId("loop-inbox-card")).toContainText("On this Mac");
  await expect(page.getByTestId("loop-approve-action")).toContainText("Continue");

  await page.getByTestId("loop-approve-action").click();

  await expect.poll(() => actionCalls.length).toBe(1);
  expect(actionCalls[0]).toEqual({
    cardId: 261,
    action: "approve_recommended_action",
    replyText: null,
  });

  await expect(page.getByTestId("loop-inbox-card-status-banner")).toContainText("This follow-up was already handled");
  await expect(page.getByText("Continue was sent to the managed local session.")).toBeVisible();
});

test("loop inbox mobile managed-local reply sends quick text to the exact card action contract (mocked tmux/session boundary)", async ({
  page,
}) => {
  const actionCalls: Array<{ cardId: number; action: string | null; replyText: string | null }> = [];

  await mockLoopInboxApi(page, {
    items: [managedLocalInboxItem],
    cardMap: { 261: managedLocalCard },
    onAction: async (payload) => {
      actionCalls.push(payload);
      return {
        body: {
          session_id: managedLocalInboxItem.session_id,
          review_id: managedLocalInboxItem.card_id,
          action: payload.action,
          status: "acted",
          reason: null,
          queued_job_id: null,
        },
      };
    },
  });

  await page.goto("/loop/card/261");

  await expect(page.getByTestId("loop-reply-box")).toBeVisible();
  await expect(page.getByTestId("loop-inbox-card")).toContainText("On this Mac");

  await page.getByTestId("loop-reply-input").fill("Continue to step 2 now.");
  await page.getByTestId("loop-reply-action").click();

  await expect.poll(() => actionCalls.length).toBe(1);
  expect(actionCalls[0]).toEqual({
    cardId: 261,
    action: "reply_to_session",
    replyText: "Continue to step 2 now.",
  });

  await expect(page.getByTestId("loop-reply-input")).toHaveValue("");
});
