import type { Page } from "@playwright/test";

import { test, expect } from "./fixtures";
import { waitForPageReady } from "../helpers/ready-signals";

const PAGE_LIMIT = 100;
const DEFAULT_TIMELINE_PATH = `/timeline?limit=${PAGE_LIMIT}`;

type TimelineSession = {
  id: string;
  provider?: string | null;
  capabilities?: {
    live_control_available?: boolean;
    host_reattach_available?: boolean;
  } | null;
};

type TimelineCard = {
  thread_id: string;
  detail: TimelineSession;
};

type TimelinePageData = {
  sessions: TimelineCard[];
  total: number;
};

function isManaged(session: TimelineSession | null | undefined): boolean {
  return Boolean(
    session?.capabilities?.live_control_available || session?.capabilities?.host_reattach_available,
  );
}

function expectedUnmanagedHint(provider: string | null | undefined): string {
  if (provider === "claude") {
    return "Restart it with longhouse claude when you want Longhouse to keep it managed and steerable.";
  }
  if (provider === "codex") {
    return "Restart it with longhouse codex when you want Longhouse to keep it managed and steerable.";
  }
  const label = provider ? provider[0].toUpperCase() + provider.slice(1) : "Session";
  return `Launch new ${label} sessions through Longhouse when you want to steer them from Longhouse.`;
}

function buildTimelinePath(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") {
      search.set(key, String(value));
    }
  }
  const query = search.toString();
  return query ? `/timeline?${query}` : "/timeline";
}

async function fetchTimelinePage(
  page: Page,
  params: Record<string, string | number | undefined>,
): Promise<TimelinePageData> {
  const apiPath = buildTimelinePath(params).replace("/timeline", "/api/timeline/sessions");
  const response = await page.evaluate(async ({ path }) => {
    const result = await fetch(path, { credentials: "include" });
    const body = await result.json().catch(() => null);
    return {
      ok: result.ok,
      status: result.status,
      body,
    };
  }, { path: apiPath });

  expect(
    response.ok,
    `timeline API should load for ${JSON.stringify(params)} (status=${response.status})`,
  ).toBeTruthy();
  const body = response.body;
  return {
    sessions: Array.isArray(body?.sessions) ? (body.sessions as TimelineCard[]) : [],
    total: typeof body?.total === "number" ? body.total : 0,
  };
}

async function openTimelinePage(page: Page, path: string): Promise<void> {
  await page.goto(path, { waitUntil: "domcontentloaded" });
  await waitForPageReady(page, { timeout: 20_000 });
  await expect(page.getByTestId("session-row").first()).toBeVisible();
}

async function findUnmanagedCard(page: Page): Promise<{ card: TimelineCard; path: string }> {
  const data = await fetchTimelinePage(page, { limit: PAGE_LIMIT });
  const card = data.sessions.find((session) => !isManaged(session.detail));
  expect(card, "Need at least one unmanaged thread in hosted timeline data").toBeTruthy();
  return { card: card!, path: DEFAULT_TIMELINE_PATH };
}

async function findAnyManagedCard(page: Page): Promise<TimelineCard | null> {
  for (const provider of ["claude", "codex"]) {
    const firstPage = await fetchTimelinePage(page, { limit: PAGE_LIMIT, provider });
    const pageCount = Math.max(1, Math.ceil(firstPage.total / PAGE_LIMIT));
    const firstHit = firstPage.sessions.find((session) => isManaged(session.detail));
    if (firstHit) {
      return firstHit;
    }

    for (let pageIndex = 1; pageIndex < pageCount; pageIndex += 1) {
      const pageData = await fetchTimelinePage(page, {
        limit: PAGE_LIMIT,
        provider,
        offset: pageIndex * PAGE_LIMIT,
      });
      const hit = pageData.sessions.find((session) => isManaged(session.detail));
      if (hit) {
        return hit;
      }
    }
  }
  return null;
}

async function findManagedCardOnVisibleTimelinePage(
  page: Page,
): Promise<{ card: TimelineCard; path: string } | null> {
  for (const provider of ["claude", "codex"]) {
    const data = await fetchTimelinePage(page, { limit: PAGE_LIMIT, provider });
    const card = data.sessions.find((session) => isManaged(session.detail));
    if (card) {
      return {
        card,
        path: buildTimelinePath({ limit: PAGE_LIMIT, provider }),
      };
    }
  }
  return null;
}

test("unmanaged sessions stay honest on hosted timeline and detail", async ({ context }) => {
  const page = await context.newPage();

  try {
    await openTimelinePage(page, DEFAULT_TIMELINE_PATH);
    const { card, path } = await findUnmanagedCard(page);

    if (path !== DEFAULT_TIMELINE_PATH) {
      await openTimelinePage(page, path);
    }

    const unmanagedRow = page.locator(`[data-testid="session-row"][data-session-id="${card.detail.id}"]`);
    await expect(unmanagedRow, "unmanaged thread row should be rendered").toBeVisible();
    // Ownership chrome moved to the detail page; the inbox row no longer surfaces it.

    await page.goto(`/timeline/${card.detail.id}`, { waitUntil: "domcontentloaded" });
    await waitForPageReady(page, { timeout: 20_000 });
    await expect(page.getByTestId("session-management-badge")).toHaveText("Unmanaged");
    await expect(page.getByTestId("session-management-summary")).toContainText("Longhouse imported this");
    await expect(page.getByTestId("session-management-summary")).toContainText(
      expectedUnmanagedHint(card.detail.provider),
    );
  } finally {
    await page.close();
  }
});

test("managed sessions stay quiet on cards and explicit on detail when present", async ({ context }) => {
  const page = await context.newPage();

  try {
    await openTimelinePage(page, buildTimelinePath({ limit: PAGE_LIMIT, provider: "claude" }));

    const anyManagedCard = await findAnyManagedCard(page);
    if (!anyManagedCard) {
      test.skip(true, "No managed threads found in hosted timeline data");
      return;
    }

    const visibleManagedCard = await findManagedCardOnVisibleTimelinePage(page);
    if (visibleManagedCard) {
      if (page.url() !== new URL(visibleManagedCard.path, page.url()).toString()) {
        await openTimelinePage(page, visibleManagedCard.path);
      }

      const managedRow = page.locator(
        `[data-testid="session-row"][data-session-id="${visibleManagedCard.card.detail.id}"]`,
      );
      await expect(managedRow, "managed thread row should be rendered").toBeVisible();
      // Ownership chrome moved to the detail page; the inbox row no longer surfaces it.
    }

    await page.goto(`/timeline/${anyManagedCard.detail.id}`, { waitUntil: "domcontentloaded" });
    await waitForPageReady(page, { timeout: 20_000 });
    await expect(page.getByTestId("session-management-badge")).toHaveText("Managed");
    await expect(page.getByTestId("session-management-summary")).toContainText(
      "Longhouse owns the control path for this session.",
    );
  } finally {
    await page.close();
  }
});
