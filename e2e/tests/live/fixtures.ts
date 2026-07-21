import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import {
  test as base,
  expect,
  type APIRequestContext,
  type BrowserContext,
  type StorageState,
} from "@playwright/test";

type RequestFactory = {
  newContext: (options?: {
    baseURL?: string;
    timeout?: number;
  }) => Promise<APIRequestContext>;
};

export function isIgnorablePlaywrightArtifactError(error: unknown): boolean {
  return (
    error instanceof Error &&
    error.message.includes("ENOENT") &&
    error.message.includes(".playwright-artifacts")
  );
}

/**
 * Wait for the hot API lane to be ready before running tests.
 * Typed archive-only degradation is acceptable when /api/readyz confirms it.
 * This prevents flaky tests during deploy windows.
 */
export async function waitForHealthy(
  requestFactory: RequestFactory,
  apiBaseUrl: string,
  options: {
    timeoutMs?: number;
    intervalMs?: number;
    requiredConsecutive?: number;
  } = {},
): Promise<void> {
  const {
    timeoutMs = 30_000,
    intervalMs = 2_000,
    requiredConsecutive = 2,
  } = options;
  const startTime = Date.now();
  let consecutiveOk = 0;
  let attempt = 0;

  const healthRequest = await requestFactory.newContext({
    baseURL: apiBaseUrl,
    timeout: 5_000,
  });

  try {
    while (Date.now() - startTime < timeoutMs) {
      attempt++;
      try {
        const response = await healthRequest.get("/api/health");
        if (response.ok()) {
          const data = await response.json();
          let ready = data.status === "healthy" || data.status === "ok";
          if (data.status === "degraded") {
            const readyResponse = await healthRequest.get("/api/readyz");
            if (readyResponse.ok()) {
              const readyData = await readyResponse.json();
              ready = readyData.status === "ready_with_archive_degraded";
            }
          }
          if (ready) {
            consecutiveOk++;
            if (consecutiveOk >= requiredConsecutive) {
              console.log(
                `[health] Ready after ${attempt} attempts (${Date.now() - startTime}ms)`,
              );
              return;
            }
          } else {
            consecutiveOk = 0;
          }
        } else {
          consecutiveOk = 0;
        }
      } catch {
        consecutiveOk = 0;
      }

      if (Date.now() - startTime + intervalMs < timeoutMs) {
        await new Promise((r) => setTimeout(r, intervalMs));
      }
    }

    console.warn(
      `[health] Timeout after ${attempt} attempts - proceeding anyway`,
    );
  } finally {
    await healthRequest.dispose().catch(() => {});
  }
}

export function normalizeToken(value: string | undefined): string | undefined {
  if (!value) return undefined;
  const trimmed = value.trim();
  if (
    (trimmed.startsWith("'") && trimmed.endsWith("'")) ||
    (trimmed.startsWith('"') && trimmed.endsWith('"'))
  ) {
    return trimmed.slice(1, -1);
  }
  return trimmed;
}

export function readDeviceToken(): string {
  if (process.env.LONGHOUSE_DEVICE_TOKEN) {
    return process.env.LONGHOUSE_DEVICE_TOKEN.trim();
  }

  try {
    return readFileSync(
      `${homedir()}/.longhouse/machine/device-token`,
      "utf8",
    ).trim();
  } catch {
    return "";
  }
}

export function buildRuntimeTokenStorageState(
  baseUrl: string,
  runtimeToken: string,
): StorageState {
  const parsed = new URL(baseUrl);
  return {
    cookies: [
      {
        name: "longhouse_session",
        value: runtimeToken,
        domain: parsed.hostname,
        path: "/",
        expires: Math.floor(Date.now() / 1000) + 3600,
        httpOnly: true,
        secure: parsed.protocol === "https:",
        sameSite: "Lax",
      },
    ],
    origins: [],
  };
}

type LiveFixtures = {
  apiBaseUrl: string;
  frontendBaseUrl: string;
  browserStorageState: StorageState;
  authToken: string;
  deviceToken: string;
  request: APIRequestContext;
  agentsRequest: APIRequestContext;
  context: BrowserContext;
};

export const test = base.extend<LiveFixtures>({
  apiBaseUrl: [
    async ({}, use) => {
      const apiBaseUrl =
        process.env.API_URL ||
        process.env.PLAYWRIGHT_API_BASE_URL ||
        process.env.E2E_API_URL ||
        "";
      await use(apiBaseUrl);
    },
    { scope: "worker" },
  ],

  frontendBaseUrl: [
    async ({ apiBaseUrl }, use) => {
      const frontendBaseUrl =
        process.env.FRONTEND_URL ||
        process.env.PLAYWRIGHT_BASE_URL ||
        process.env.E2E_FRONTEND_URL ||
        apiBaseUrl;
      await use(frontendBaseUrl);
    },
    { scope: "worker" },
  ],

  browserStorageState: [
    async ({ apiBaseUrl, playwright }, use) => {
      const runtimeToken = normalizeToken(process.env.SMOKE_RUNTIME_TOKEN) || readDeviceToken();
      if (runtimeToken) {
        await waitForHealthy(playwright.request, apiBaseUrl);
        await use(buildRuntimeTokenStorageState(apiBaseUrl, runtimeToken));
        return;
      }

      test.skip(true, "SMOKE_RUNTIME_TOKEN or LONGHOUSE_DEVICE_TOKEN not set; skipping live prod E2E");
    },
    { scope: "worker" },
  ],

  authToken: [
    async ({ apiBaseUrl, playwright }, use) => {
      if (!process.env.RUN_LIVE_E2E) {
        test.skip(true, "RUN_LIVE_E2E not set; skipping live prod E2E");
      }

      if (!apiBaseUrl) {
        test.skip(
          true,
          "API_URL or PLAYWRIGHT_API_BASE_URL required; skipping live prod E2E",
        );
      }

      const runtimeToken = normalizeToken(process.env.SMOKE_RUNTIME_TOKEN) || readDeviceToken();
      if (runtimeToken) {
        await waitForHealthy(playwright.request, apiBaseUrl);
        await use(runtimeToken);
        return;
      }

      test.skip(true, "SMOKE_RUNTIME_TOKEN or LONGHOUSE_DEVICE_TOKEN not set; skipping live prod E2E");
    },
    { scope: "worker" },
  ],

  deviceToken: [
    async ({}, use) => {
      await use(readDeviceToken());
    },
    { scope: "worker" },
  ],

  request: async ({ playwright, apiBaseUrl, authToken }, use) => {
    const request = await playwright.request.newContext({
      baseURL: apiBaseUrl,
      extraHTTPHeaders: {
        Authorization: `Bearer ${authToken}`,
      },
      timeout: 45_000,
    });
    await use(request);
    await request.dispose().catch((error) => {
      if (!isIgnorablePlaywrightArtifactError(error)) {
        throw error;
      }
    });
  },

  agentsRequest: async ({ playwright, apiBaseUrl, deviceToken }, use) => {
    // `/api/agents/*` uses the explicit device-token header. The browser fixture
    // may use that same owner-bound token as its session cookie because hosted
    // auth resolves `zdt_` principals through the canonical device-token store.
    const extraHTTPHeaders: Record<string, string> = {};
    if (deviceToken) {
      extraHTTPHeaders["X-Agents-Token"] = deviceToken;
    }

    const request = await playwright.request.newContext({
      baseURL: apiBaseUrl,
      extraHTTPHeaders,
      timeout: 45_000,
    });
    await use(request);
    await request.dispose().catch((error) => {
      if (!isIgnorablePlaywrightArtifactError(error)) {
        throw error;
      }
    });
  },

  context: async ({ browser, frontendBaseUrl, browserStorageState }, use) => {
    const context = await browser.newContext({
      baseURL: frontendBaseUrl,
      storageState: browserStorageState,
    });

    try {
      await use(context);
    } finally {
      await context.close().catch((error) => {
        if (!isIgnorablePlaywrightArtifactError(error)) {
          throw error;
        }
      });
    }
  },
});

export { expect } from "@playwright/test";
