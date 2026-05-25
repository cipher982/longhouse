import { randomUUID } from "crypto";
import type { APIRequestContext, Page } from "@playwright/test";
import { test, expect } from "../fixtures";
import { resetDatabase } from "../test-utils";

type IngestEvent = {
  role: "user" | "assistant" | "tool";
  content_text?: string | null;
  tool_name?: string | null;
  tool_input_json?: Record<string, unknown> | null;
  tool_output_text?: string | null;
  tool_call_id?: string | null;
  timestamp: string;
  source_path: string;
  source_offset: number;
};

async function ingestSessionEvents(
  request: APIRequestContext,
  options: {
    sessionId: string;
    project: string;
    events: IngestEvent[];
    startedAt: string;
  },
): Promise<void> {
  const response = await request.post("/api/agents/ingest", {
    data: {
      id: options.sessionId,
      provider: "claude",
      environment: "e2e-machine",
      project: options.project,
      device_id: `device-${options.sessionId.slice(0, 8)}`,
      device_name: "Cinder",
      cwd: "/tmp/longhouse-test",
      git_repo: null,
      git_branch: null,
      provider_session_id: `claude-${options.sessionId}`,
      started_at: options.startedAt,
      ended_at: null,
      events: options.events,
    },
  });

  expect(
    response.ok(),
    `session ingest failed: ${response.status()} ${await response.text()}`,
  ).toBe(true);
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
        managed_transport: "claude_channel_bridge",
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
  ).toBe(true);
}

async function sendAttachedControlLease(
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
          provider: "claude",
          machine_id: "cinder",
          sequence: Date.now(),
          state: "attached",
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

async function sendProviderBlockedPhase(
  request: APIRequestContext,
  sessionId: string,
  occurredAt: string,
): Promise<void> {
  const response = await request.post("/api/agents/runtime/events/batch", {
    data: {
      events: [
        {
          runtime_key: `claude:${sessionId}`,
          session_id: sessionId,
          provider: "claude",
          device_id: `device-${sessionId.slice(0, 8)}`,
          source: "e2e",
          kind: "phase_signal",
          phase: "blocked",
          tool_name: "AskUserQuestion",
          occurred_at: occurredAt,
          freshness_ms: 24 * 60 * 60 * 1000,
          dedupe_key: `blocked-phase-${sessionId}-${Date.parse(occurredAt)}`,
          payload: {},
        },
      ],
    },
  });

  expect(
    response.ok(),
    `runtime ingest failed: ${response.status()} ${await response.text()}`,
  ).toBe(true);
}

async function getSession(request: APIRequestContext, sessionId: string): Promise<any> {
  const response = await request.get(`/api/agents/sessions/${sessionId}`);
  expect(
    response.ok(),
    `get session failed: ${response.status()} ${await response.text()}`,
  ).toBe(true);
  return response.json();
}

async function getRecentRenderBeacons(
  request: APIRequestContext,
  sessionId: string,
): Promise<Record<string, unknown>[]> {
  const response = await request.get(
    `/api/telemetry/client-render/recent?session_id=${sessionId}`,
  );
  expect(
    response.ok(),
    `recent render beacons failed: ${response.status()} ${await response.text()}`,
  ).toBe(true);
  const body = (await response.json()) as { items?: Record<string, unknown>[] };
  return body.items ?? [];
}

async function installWorkspaceFrameProbe(page: Page, sessionId: string): Promise<void> {
  await page.addInitScript((targetSessionId) => {
    const globalWindow = window as unknown as {
      __workspaceFrames__?: Record<string, unknown>[];
      EventSource: typeof EventSource;
    };
    globalWindow.__workspaceFrames__ = [];

    const OriginalEventSource = globalWindow.EventSource;
    class PatchedEventSource extends OriginalEventSource {
      constructor(url: string | URL, init?: EventSourceInit) {
        super(url, init);
        const urlStr = typeof url === "string" ? url : url.toString();
        if (!urlStr.includes(`/sessions/${targetSessionId}/workspace/stream`)) {
          return;
        }

        this.addEventListener("workspace_changed", (evt: MessageEvent) => {
          let payload: Record<string, unknown> | null = null;
          try {
            payload = JSON.parse(evt.data);
          } catch {
            payload = null;
          }
          requestAnimationFrame(() => {
            globalWindow.__workspaceFrames__?.push({
              arrivedAtMs: performance.now(),
              latestEventId: payload?.latest_event_id ?? null,
              pubsubSeq: payload?.pubsub_seq ?? null,
            });
          });
        });
      }
    }
    globalWindow.EventSource = PatchedEventSource as unknown as typeof EventSource;
  }, sessionId);
}

test.describe("Session hot plane", () => {
  test.beforeEach(async ({ request }) => {
    await resetDatabase(request);
  });

  test("AskUserQuestion answer clears blocked UI through workspace SSE", async ({
    page,
    request,
  }) => {
    test.setTimeout(60_000);

    const suffix = randomUUID().slice(0, 8);
    const sessionId = randomUUID();
    const project = `ask-user-hot-plane-${suffix}`;
    const start = new Date(Date.now() - 60_000).toISOString();
    const blockedAt = new Date(Date.now() - 30_000).toISOString();
    const answerAt = new Date().toISOString();
    const sourcePath = `/tmp/${sessionId}.jsonl`;

    await ingestSessionEvents(request, {
      sessionId,
      project,
      startedAt: start,
      events: [
        {
          role: "user",
          content_text: `choose-path-${suffix}`,
          timestamp: start,
          source_path: sourcePath,
          source_offset: 0,
        },
        {
          role: "assistant",
          content_text: null,
          tool_name: "AskUserQuestion",
          tool_call_id: "toolu_ask_user",
          tool_input_json: {
            question: "How should I fix the drag feel?",
            choices: ["Use dnd-kit", "Keep inset line"],
          },
          timestamp: blockedAt,
          source_path: sourcePath,
          source_offset: 200,
        },
      ],
    });
    await configureManagedLocalSession(request, sessionId);
    await sendAttachedControlLease(request, sessionId);
    await sendProviderBlockedPhase(request, sessionId, blockedAt);

    await installWorkspaceFrameProbe(page, sessionId);
    await page.goto(`/timeline/${sessionId}`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector('body[data-ready="true"]', { timeout: 15_000 });

    const strip = page.getByTestId("session-control-strip");
    await expect(strip).toContainText("Needs permission", { timeout: 10_000 });
    await expect(strip).toContainText("Approval needed", { timeout: 10_000 });
    await expect(strip).toContainText("AskUserQuestion", { timeout: 10_000 });

    const blockedSession = await getSession(request, sessionId);
    expect(blockedSession.runtime_display?.state).toBe("blocked");
    expect(blockedSession.runtime_display?.needs_attention).toBe(true);

    await ingestSessionEvents(request, {
      sessionId,
      project,
      startedAt: start,
      events: [
        {
          role: "tool",
          tool_call_id: "toolu_ask_user",
          tool_output_text: "User has answered your questions: Use dnd-kit.",
          timestamp: answerAt,
          source_path: sourcePath,
          source_offset: 400,
        },
      ],
    });

    await expect(
      page
        .getByTestId("session-timeline-row")
        .filter({ hasText: "Use dnd-kit" })
        .last(),
    ).toBeVisible({ timeout: 5_000 });
    await expect(strip).not.toContainText("Blocked", { timeout: 5_000 });
    await expect(strip).not.toContainText("AskUserQuestion", { timeout: 5_000 });

    await expect
      .poll(async () => {
        return page.evaluate(() => {
          const win = window as unknown as { __workspaceFrames__?: unknown[] };
          return win.__workspaceFrames__?.length ?? 0;
        });
      }, { timeout: 5_000 })
      .toBeGreaterThan(0);

    await expect
      .poll(async () => getRecentRenderBeacons(request, sessionId), { timeout: 5_000 })
      .toEqual(
        expect.arrayContaining([
          expect.objectContaining({
            session_id: sessionId,
            surface: "web",
            emitted_at_ms: Date.parse(answerAt),
          }),
        ]),
      );
    const renderBeacon = (await getRecentRenderBeacons(request, sessionId))[0];
    expect(renderBeacon).toMatchObject({
      session_id: sessionId,
      surface: "web",
      emitted_at_ms: Date.parse(answerAt),
    });

    const clearedSession = await getSession(request, sessionId);
    expect(clearedSession.runtime_display?.signal_tier).toBe("transcript_progress");
    expect(clearedSession.runtime_display?.state).toBeNull();
    expect(clearedSession.runtime_display?.needs_attention).toBe(false);
  });
});
