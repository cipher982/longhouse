import { randomUUID } from "crypto";
import type { APIRequestContext, Page } from "@playwright/test";
import { test, expect } from "../fixtures";
import { resetDatabase } from "../test-utils";

type IngestOverrides = Partial<{
  id: string;
  provider: string;
  project: string;
  device_id: string;
  device_name: string;
  cwd: string;
  git_repo: string | null;
  git_branch: string | null;
  started_at: string;
  ended_at: string | null;
}>;

async function ingestSession(
  request: APIRequestContext,
  overrides: IngestOverrides = {},
): Promise<string> {
  const sessionId = overrides.id || randomUUID();
  const timestamp = overrides.started_at || new Date().toISOString();
  const response = await request.post("/api/agents/ingest", {
    data: {
      id: sessionId,
      provider: overrides.provider || "codex",
      environment: "e2e",
      project: overrides.project || "session-messages-e2e",
      device_id: overrides.device_id || `device-${sessionId.slice(0, 8)}`,
      device_name: overrides.device_name || `Device ${sessionId.slice(0, 4)}`,
      cwd: overrides.cwd || "/Users/example/git/zerg",
      git_repo:
        overrides.git_repo === undefined
          ? "git@github.com:cipher982/longhouse.git"
          : overrides.git_repo,
      git_branch:
        overrides.git_branch === undefined ? "main" : overrides.git_branch,
      started_at: timestamp,
      ended_at:
        overrides.ended_at === undefined ? timestamp : overrides.ended_at,
      provider_session_id: `provider-${sessionId}`,
      events: [
        {
          role: "user",
          content_text: `bootstrap ${sessionId}`,
          timestamp,
          source_path: `/tmp/${sessionId}.jsonl`,
          source_offset: 0,
        },
      ],
    },
  });

  expect(
    response.ok(),
    `session ingest failed: ${response.status()} ${await response.text()}`,
  ).toBeTruthy();
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
        clear_ended_at: true,
      },
    },
  );

  expect(
    response.ok(),
    `managed-local config failed: ${response.status()} ${await response.text()}`,
  ).toBeTruthy();
}

async function sendPresence(
  request: APIRequestContext,
  sessionId: string,
  state: "idle" | "thinking" | "running" | "needs_user" | "blocked",
): Promise<void> {
  const response = await request.post("/api/agents/presence", {
    data: {
      session_id: sessionId,
      state,
      cwd: "/Users/example/git/zerg",
      provider: "codex",
    },
  });

  expect(
    response.status(),
    `presence update failed: ${await response.text()}`,
  ).toBe(204);
}

async function sendSessionMessage(
  request: APIRequestContext,
  options: {
    fromSessionId: string;
    toSessionId: string;
    text: string;
  },
): Promise<any> {
  const { fromSessionId, toSessionId, text } = options;
  const response = await request.post("/api/agents/messages", {
    headers: {
      "X-Longhouse-Session-Id": fromSessionId,
    },
    data: {
      to_session_id: toSessionId,
      text,
    },
  });

  expect(
    response.ok(),
    `message send failed: ${response.status()} ${await response.text()}`,
  ).toBeTruthy();
  return response.json();
}

async function listInboundMessages(
  request: APIRequestContext,
  options: {
    sessionId: string;
    unacknowledgedOnly?: boolean;
  },
): Promise<any> {
  const { sessionId, unacknowledgedOnly = false } = options;
  const response = await request.get("/api/agents/messages", {
    headers: {
      "X-Longhouse-Session-Id": sessionId,
    },
    params: {
      direction: "inbound",
      unacknowledged_only: String(unacknowledgedOnly),
      limit: "20",
    },
  });

  expect(
    response.ok(),
    `message list failed: ${response.status()} ${await response.text()}`,
  ).toBeTruthy();
  return response.json();
}

async function acknowledgeMessage(
  request: APIRequestContext,
  options: {
    sessionId: string;
    messageId: number;
  },
): Promise<any> {
  const { sessionId, messageId } = options;
  const response = await request.post(`/api/agents/messages/${messageId}/ack`, {
    headers: {
      "X-Longhouse-Session-Id": sessionId,
    },
  });

  expect(
    response.ok(),
    `message ack failed: ${response.status()} ${await response.text()}`,
  ).toBeTruthy();
  return response.json();
}

async function openSessionDetail(page: Page, sessionId: string): Promise<void> {
  await page.goto(`/timeline/${sessionId}`);
  await page.waitForSelector('body[data-ready="true"]', { timeout: 15000 });
}

test.describe("Session messages", () => {
  test.beforeEach(async ({ request }) => {
    await resetDatabase(request);
  });

  test("managed-local target delivers immediately and the workspace refreshes with the injected message", async ({
    page,
    request,
  }) => {
    test.setTimeout(90_000);

    const sourceSessionId = await ingestSession(request, {
      provider: "claude",
      device_id: "sender-device",
      device_name: "Sender",
    });
    const targetSessionId = await ingestSession(request, {
      provider: "codex",
      device_id: "target-device",
      device_name: "Target",
      ended_at: null,
    });

    await configureManagedLocalSession(request, targetSessionId);
    await sendPresence(request, targetSessionId, "idle");
    await openSessionDetail(page, targetSessionId);

    await expect(page.getByTestId("session-continuation-panel")).toBeVisible();

    const messageText = `immediate-${randomUUID().slice(0, 8)}`;
    const sendResult = await sendSessionMessage(request, {
      fromSessionId: sourceSessionId,
      toSessionId: targetSessionId,
      text: messageText,
    });

    expect(sendResult.delivery_status).toBe("delivered");

    const row = page
      .getByTestId("session-timeline-row")
      .filter({ hasText: messageText })
      .last();
    await expect(row).toBeVisible({ timeout: 20_000 });

    await expect
      .poll(async () => {
        const payload = await listInboundMessages(request, {
          sessionId: targetSessionId,
        });
        return payload.messages[0]?.delivery_status;
      })
      .toBe("delivered");
  });

  test("running target queues until a safe presence boundary, then delivers", async ({
    page,
    request,
  }) => {
    test.setTimeout(90_000);

    const sourceSessionId = await ingestSession(request, {
      provider: "claude",
      device_id: "sender-device",
      device_name: "Sender",
    });
    const targetSessionId = await ingestSession(request, {
      provider: "codex",
      device_id: "target-device",
      device_name: "Target",
      ended_at: null,
    });

    await configureManagedLocalSession(request, targetSessionId);
    await sendPresence(request, targetSessionId, "running");
    await openSessionDetail(page, targetSessionId);

    const messageText = `queued-${randomUUID().slice(0, 8)}`;
    const sendResult = await sendSessionMessage(request, {
      fromSessionId: sourceSessionId,
      toSessionId: targetSessionId,
      text: messageText,
    });

    expect(sendResult.delivery_status).toBe("queued");
    await expect(
      page.getByTestId("session-timeline-row").filter({ hasText: messageText }),
    ).toHaveCount(0);

    const prematureAck = await request.post(
      `/api/agents/messages/${sendResult.id}/ack`,
      {
        headers: {
          "X-Longhouse-Session-Id": targetSessionId,
        },
      },
    );
    expect(prematureAck.status()).toBe(409);

    await sendPresence(request, targetSessionId, "thinking");

    await expect
      .poll(async () => {
        const payload = await listInboundMessages(request, {
          sessionId: targetSessionId,
        });
        return payload.messages[0]?.delivery_status;
      })
      .toBe("delivered");

    const row = page
      .getByTestId("session-timeline-row")
      .filter({ hasText: messageText })
      .last();
    await expect(row).toBeVisible({ timeout: 20_000 });
  });

  test("legacy target falls back to stored_only and requires explicit acknowledgement", async ({
    request,
  }) => {
    const sourceSessionId = await ingestSession(request, {
      provider: "claude",
      device_id: "sender-device",
      device_name: "Sender",
    });
    const targetSessionId = await ingestSession(request, {
      provider: "codex",
      device_id: "target-device",
      device_name: "Target",
    });

    const messageText = `stored-only-${randomUUID().slice(0, 8)}`;
    const sendResult = await sendSessionMessage(request, {
      fromSessionId: sourceSessionId,
      toSessionId: targetSessionId,
      text: messageText,
    });

    expect(sendResult.delivery_status).toBe("stored_only");
    expect(sendResult.acknowledged_at).toBeNull();

    await expect
      .poll(async () => {
        const payload = await listInboundMessages(request, {
          sessionId: targetSessionId,
          unacknowledgedOnly: true,
        });
        return payload.total;
      })
      .toBe(1);

    const ackResult = await acknowledgeMessage(request, {
      sessionId: targetSessionId,
      messageId: sendResult.id,
    });
    expect(ackResult.acknowledged_at).toBeTruthy();

    await expect
      .poll(async () => {
        const payload = await listInboundMessages(request, {
          sessionId: targetSessionId,
          unacknowledgedOnly: true,
        });
        return payload.total;
      })
      .toBe(0);
  });
});
