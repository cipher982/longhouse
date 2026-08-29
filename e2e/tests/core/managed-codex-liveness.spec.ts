import { randomUUID } from "crypto";
import type { APIRequestContext } from "@playwright/test";
import { test, expect } from "../fixtures";
import { ingestStorageV2Session } from "../storage-v2-fixtures";

async function ingestCodexSession(
  request: APIRequestContext,
  options: {
    project: string;
    token: string;
    timestamp: string;
  },
): Promise<string> {
  const sessionId = randomUUID();
  await ingestStorageV2Session(request, {
    sessionId,
    provider: "codex",
    environment: "e2e-machine",
    project: options.project,
    cwd: "/tmp",
    providerSessionId: `codex-session-${sessionId}`,
    startedAt: options.timestamp,
    endedAt: options.timestamp,
    events: [
      {
        role: "user",
        content_text: options.token,
        timestamp: options.timestamp,
      },
    ],
  });
  return sessionId;
}

async function configureManagedLocalSession(
  request: APIRequestContext,
  sessionId: string,
  project: string,
  observedAt: string,
): Promise<void> {
  const response = await request.post(
    `/api/admin/test/sessions/${sessionId}/runtime`,
    {
      data: {
        provider: "codex",
        project,
        execution_home: "managed_local",
        managed_transport: "codex_app_server",
        source_runner_id: 77,
        source_runner_name: "Cinder",
        managed_session_name: `lh-e2e-${sessionId.slice(0, 8)}`,
        observed_at: observedAt,
      },
    },
  );

  expect(
    response.ok(),
    `managed-local config failed: ${response.status()} ${await response.text()}`,
  ).toBe(true);
}

async function getSession(
  request: APIRequestContext,
  sessionId: string,
): Promise<any> {
  // Control and capability state is the browser projection. The machine
  // surface serves an archival payload that deliberately omits it.
  const response = await request.get(`/api/timeline/sessions/${sessionId}`);
  expect(
    response.ok(),
    `get session failed: ${response.status()} ${await response.text()}`,
  ).toBe(true);
  return response.json();
}

test.describe("Managed Codex liveness", () => {
  test("managed ownership does not fabricate activity without runtime evidence", async ({
    page,
    request,
  }) => {
    const suffix = randomUUID().slice(0, 8);
    const project = `managed-idle-lease-${suffix}`;
    const token = `managed-idle-ready-${suffix}`;
    const oldTimestamp = new Date(
      Date.now() - 2 * 60 * 60 * 1000,
    ).toISOString();

    const sessionId = await ingestCodexSession(request, {
      project,
      token,
      timestamp: oldTimestamp,
    });
    await configureManagedLocalSession(
      request,
      sessionId,
      project,
      oldTimestamp,
    );

    const session = await getSession(request, sessionId);
    expect(session.session_state?.mode).toBe("helm");
    expect(session.session_state?.control?.ownership).toBe("owned");
    expect(session.runtime_display?.control_path).toBe("managed");
    expect(session.runtime_display?.state).toBeNull();
    expect(session.capabilities?.live_control_available).toBe(false);
    expect(session.capabilities?.composer_enabled).toBe(false);

    await page.goto(`/timeline?project=${project}`);
    await page.waitForSelector('[data-ready="true"]', { timeout: 10000 });

    const row = page.locator(
      `[data-testid="session-row"][data-session-id="${sessionId}"]`,
    );
    await expect(row).toBeVisible();
    await expect(row).toHaveAttribute("data-closed", "false");
  });
});
