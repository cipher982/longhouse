import { randomUUID } from "crypto";
import type { APIRequestContext } from "@playwright/test";
import { test, expect } from "../fixtures";

async function ingestCodexSession(
  request: APIRequestContext,
  options: {
    project: string;
    token: string;
    timestamp: string;
  },
): Promise<string> {
  const sessionId = randomUUID();
  const response = await request.post("/api/agents/ingest", {
    data: {
      id: sessionId,
      provider: "codex",
      environment: "e2e-machine",
      project: options.project,
      device_id: "e2e-device",
      cwd: "/tmp",
      git_repo: null,
      git_branch: null,
      provider_session_id: `codex-session-${sessionId}`,
      started_at: options.timestamp,
      ended_at: options.timestamp,
      events: [
        {
          role: "user",
          content_text: options.token,
          timestamp: options.timestamp,
          source_path: "/tmp/managed-codex-liveness.jsonl",
          source_offset: 0,
        },
      ],
    },
  });

  expect(
    response.ok(),
    `session ingest failed: ${response.status()} ${await response.text()}`,
  ).toBe(true);
  return sessionId;
}

async function configureManagedLocalSession(
  request: APIRequestContext,
  sessionId: string,
): Promise<void> {
  const response = await request.post(
    `/api/admin/test/sessions/${sessionId}/runtime`,
    {
      data: {
        execution_home: "managed_local",
        managed_transport: "codex_app_server",
        source_runner_id: 77,
        source_runner_name: "Cinder",
        managed_session_name: `lh-e2e-${sessionId.slice(0, 8)}`,
        clear_ended_at: false,
      },
    },
  );

  expect(
    response.ok(),
    `managed-local config failed: ${response.status()} ${await response.text()}`,
  ).toBe(true);
}

async function sendAttachedIdleLease(
  request: APIRequestContext,
  sessionId: string,
): Promise<void> {
  const response = await request.post("/api/agents/heartbeat", {
    data: {
      version: "e2e",
      daemon_pid: 123,
      managed_sessions: [
        {
          session_id: sessionId,
          provider: "codex",
          machine_id: "cinder",
          sequence: Date.now(),
          state: "attached",
          phase: "idle",
          bridge_status: "ready",
          thread_subscription_status: "subscribed",
          observed_at: new Date().toISOString(),
          lease_ttl_ms: 15 * 60 * 1000,
        },
      ],
    },
  });

  expect(
    response.status(),
    `heartbeat failed: ${response.status()} ${await response.text()}`,
  ).toBe(204);
}

test.describe("Managed Codex liveness", () => {
  test("timeline keeps attached idle lease ready despite old transcript ended_at", async ({
    page,
    request,
  }) => {
    const suffix = randomUUID().slice(0, 8);
    const project = `managed-idle-lease-${suffix}`;
    const token = `managed-idle-ready-${suffix}`;
    const oldTimestamp = new Date(Date.now() - 2 * 60 * 60 * 1000).toISOString();

    const sessionId = await ingestCodexSession(request, {
      project,
      token,
      timestamp: oldTimestamp,
    });
    await configureManagedLocalSession(request, sessionId);
    await sendAttachedIdleLease(request, sessionId);

    await page.goto(`/timeline?project=${project}`);
    await page.waitForSelector('[data-ready="true"]', { timeout: 10000 });

    const row = page
      .getByTestId("session-row")
      .filter({ hasText: token })
      .first();
    await expect(row).toBeVisible();
    await expect(row).toHaveAttribute("data-status", "idle");
    await expect(row).toContainText("idle");
  });
});
