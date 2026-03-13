import { expect, test, type Page } from "@playwright/test";

type GmailUser = {
  id: number;
  email: string;
  display_name: string | null;
  avatar_url: string | null;
  is_active: boolean;
  created_at: string;
  last_login: string | null;
  prefs: Record<string, unknown>;
  role: string;
  gmail_connected: boolean;
  gmail_mailbox_email: string | null;
  gmail_watch_status: "active" | "failed" | "not_configured" | null;
  gmail_watch_error: string | null;
  gmail_watch_expiry: number | null;
};

type GoogleClientMode = "success" | "cancel";

function buildUser(overrides: Partial<GmailUser> = {}): GmailUser {
  return {
    id: 1,
    email: "owner@example.com",
    display_name: "Owner",
    avatar_url: null,
    is_active: true,
    created_at: "2026-03-12T18:00:00Z",
    last_login: null,
    prefs: {},
    role: "USER",
    gmail_connected: false,
    gmail_mailbox_email: null,
    gmail_watch_status: null,
    gmail_watch_error: null,
    gmail_watch_expiry: null,
    ...overrides,
  };
}

function buildRuntimeConfig(googleClientId: string): string {
  return [
    'window.API_BASE_URL="/api";',
    'window.WS_BASE_URL="ws://127.0.0.1:47300/api/ws";',
    'window.__APP_MODE__="production";',
    "window.__SINGLE_TENANT__=true;",
    `window.__GOOGLE_CLIENT_ID__=${JSON.stringify(googleClientId)};`,
    "window.__LLM_AVAILABLE__=true;",
    "window.__EMBEDDINGS_AVAILABLE__=true;",
  ].join("\n");
}

async function mockRuntimeConfig(page: Page, googleClientId: string): Promise<void> {
  await page.route("**/config.js*", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/javascript",
      body: buildRuntimeConfig(googleClientId),
    });
  });
}

async function mockGoogleIdentity(page: Page, mode: GoogleClientMode, code = "auth-code"): Promise<void> {
  await page.addInitScript(
    ({ clientMode, authCode }) => {
      const oauth2 = {
        initCodeClient: (config: {
          callback: (response: { code?: string; error?: string; error_description?: string }) => void;
          error_callback?: (error: { type?: string; message?: string }) => void;
        }) => ({
          requestCode: () => {
            queueMicrotask(() => {
              if (clientMode === "success") {
                config.callback({ code: authCode });
                return;
              }
              config.error_callback?.({
                type: "popup_closed_by_user",
                message: "Popup closed by user.",
              });
            });
          },
        }),
      };

      Object.defineProperty(window, "google", {
        value: { accounts: { oauth2 } },
        configurable: true,
      });
    },
    { clientMode: mode, authCode: code },
  );
}

async function mockInboxApis(
  page: Page,
  options: {
    getUser: () => GmailUser;
    onConnect?: (requestBody: unknown) => { responseBody: Record<string, unknown> };
  },
): Promise<{ connectBodies: unknown[] }> {
  const connectBodies: unknown[] = [];

  await page.route("**/api/auth/status", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        authenticated: true,
        user: options.getUser(),
      }),
    });
  });

  await page.route("**/api/conversations**", async (route) => {
    const url = new URL(route.request().url());
    const pathname = url.pathname;

    if (pathname.endsWith("/conversations") || pathname.endsWith("/conversations/search")) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          conversations: [],
          total: 0,
        }),
      });
      return;
    }

    if (pathname.endsWith("/messages")) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          messages: [],
          total: 0,
        }),
      });
      return;
    }

    await route.fulfill({
      status: 404,
      contentType: "application/json",
      body: JSON.stringify({ detail: `Unhandled conversations route: ${pathname}` }),
    });
  });

  await page.route("**/api/auth/google/gmail", async (route) => {
    const requestBody = route.request().postDataJSON();
    connectBodies.push(requestBody);

    if (!options.onConnect) {
      await route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({ detail: "Unexpected Gmail connect call" }),
      });
      return;
    }

    const result = options.onConnect(requestBody);

    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(result.responseBody),
    });
  });

  return { connectBodies };
}

async function openInbox(page: Page): Promise<void> {
  await page.goto("/conversations");
  await page.locator("body[data-ready='true']").waitFor({ timeout: 15000 });
  await expect(page.getByTestId("gmail-connection-panel")).toBeVisible();
}

test.describe("Gmail inbox onboarding", () => {
  test("makes missing Google OAuth explicit before inbox setup", async ({ page }) => {
    let currentUser = buildUser();

    await mockRuntimeConfig(page, "");
    await mockInboxApis(page, {
      getUser: () => currentUser,
    });

    await openInbox(page);

    await expect(page.getByText("Connect Gmail to start your inbox")).toBeVisible();
    await expect(
      page.getByText("Google OAuth is not configured on this instance yet. Add a Google client first, then connect Gmail here."),
    ).toBeVisible();
    await expect(page.getByRole("button", { name: "Connect Gmail" })).toBeDisabled();
    await expect(
      page.getByText("Ask the instance admin to configure Google OAuth, then connect Gmail here."),
    ).toBeVisible();
  });

  test("connects Gmail from the inbox through the popup consent flow", async ({ page }) => {
    let currentUser = buildUser();

    await mockRuntimeConfig(page, "google-client-id");
    await mockGoogleIdentity(page, "success", "gmail-auth-code");
    const { connectBodies } = await mockInboxApis(page, {
      getUser: () => currentUser,
      onConnect: (requestBody) => {
        currentUser = buildUser({
          gmail_connected: true,
          gmail_mailbox_email: "owner@gmail.com",
          gmail_watch_status: "active",
          gmail_watch_expiry: 654321,
        });
        return {
          responseBody: {
            status: "connected",
            connector_id: 1,
            mailbox_email: "owner@gmail.com",
            watch: {
              status: "active",
              method: "pubsub",
              history_id: 321,
              watch_expiry: 654321,
              error: null,
            },
          },
        };
      },
    });

    await openInbox(page);

    await page.getByRole("button", { name: "Connect Gmail" }).click();

    await expect(page.getByText("Email sync is healthy")).toBeVisible();
    await expect(page.getByText("Connected as owner@gmail.com")).toBeVisible();
    await expect(page.getByText("Ready")).toBeVisible();
    await expect(page.getByRole("button", { name: "Connect Gmail" })).toHaveCount(0);

    expect(connectBodies).toHaveLength(1);
    expect(connectBodies[0]).toEqual({ auth_code: "gmail-auth-code" });
  });

  test("surfaces partial Gmail bootstrap failure with reconnect guidance", async ({ page }) => {
    let currentUser = buildUser();

    await mockRuntimeConfig(page, "google-client-id");
    await mockGoogleIdentity(page, "success");
    const { connectBodies } = await mockInboxApis(page, {
      getUser: () => currentUser,
      onConnect: () => {
        currentUser = buildUser({
          gmail_connected: true,
          gmail_mailbox_email: "owner@gmail.com",
          gmail_watch_status: "failed",
          gmail_watch_error: "Reconnect Gmail to finish email sync.",
        });
        return {
          responseBody: {
            status: "connected",
            connector_id: 1,
            mailbox_email: "owner@gmail.com",
            watch: {
              status: "failed",
              method: "pubsub",
              error: "Started Gmail watch but could not resolve mailbox email for Pub/Sub routing.",
            },
          },
        };
      },
    });

    await openInbox(page);

    await page.getByRole("button", { name: "Connect Gmail" }).click();

    await expect(page.getByText("Gmail needs attention")).toBeVisible();
    await expect(page.getByText("Reconnect Gmail to finish email sync.")).toBeVisible();
    await expect(page.getByText("Connected as owner@gmail.com")).toBeVisible();
    await expect(page.getByRole("button", { name: "Reconnect Gmail" })).toBeVisible();

    expect(connectBodies).toHaveLength(1);
  });

  test("keeps the user in the inbox when Google consent is cancelled", async ({ page }) => {
    let currentUser = buildUser();

    await mockRuntimeConfig(page, "google-client-id");
    await mockGoogleIdentity(page, "cancel");
    const { connectBodies } = await mockInboxApis(page, {
      getUser: () => currentUser,
    });

    await openInbox(page);

    await page.getByRole("button", { name: "Connect Gmail" }).click();

    await expect(page.getByText("Popup closed by user.")).toBeVisible();
    await expect(page.getByRole("button", { name: "Connect Gmail" })).toBeVisible();
    await expect(page.getByText("Connect Gmail to start your inbox")).toBeVisible();
    expect(connectBodies).toHaveLength(0);
  });
});
