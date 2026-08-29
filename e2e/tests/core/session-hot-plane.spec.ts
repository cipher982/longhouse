import { randomUUID } from "crypto";
import type { APIRequestContext, Page } from "@playwright/test";
import { test, expect } from "../fixtures";
import { ingestStorageV2Session } from "../storage-v2-fixtures";
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
  await ingestStorageV2Session(request, {
    sessionId: options.sessionId,
    provider: "claude",
    environment: "e2e-machine",
    project: options.project,
    cwd: "/tmp/longhouse-test",
    providerSessionId: `claude-${options.sessionId}`,
    startedAt: options.startedAt,
    endedAt: null,
    events: options.events,
  });
}

async function configureManagedLocalSession(
  request: APIRequestContext,
  sessionId: string,
  project: string,
): Promise<void> {
  const response = await request.post(
    `/api/admin/test/sessions/${sessionId}/runtime`,
    {
      data: {
        provider: "claude",
        project,
        cwd: "/tmp/longhouse-test",
        execution_home: "managed_local",
        managed_transport: "claude_channel_bridge",
        source_runner_id: 77,
        source_runner_name: "Cinder",
        managed_session_name: `lh-e2e-${sessionId.slice(0, 8)}`,
      },
    },
  );

  expect(
    response.ok(),
    `managed-local config failed: ${response.status()} ${await response.text()}`,
  ).toBe(true);
}

async function publishInteraction(
  request: APIRequestContext,
  options: {
    sessionId: string;
    kind: "pause_request" | "pause_resolution";
    occurredAt: string;
    requestKey: string;
    toolCallId: string;
    responseText?: string;
  },
): Promise<void> {
  const response = await request.post("/api/agents/runtime/events/batch", {
    data: {
      events: [
        {
          runtime_key: `claude:${options.sessionId}`,
          session_id: options.sessionId,
          provider: "claude",
          device_id: "Cinder",
          source: "e2e",
          kind: options.kind,
          tool_name: "AskUserQuestion",
          occurred_at: options.occurredAt,
          dedupe_key: `${options.kind}:${options.requestKey}`,
          payload:
            options.kind === "pause_request"
              ? {
                  request_key: options.requestKey,
                  provider_request_id: options.toolCallId,
                  kind: "structured_question",
                  tool_name: "AskUserQuestion",
                  title: "How should I fix the drag feel?",
                  summary: "Waiting for your answer.",
                  request_payload: {
                    question: "How should I fix the drag feel?",
                    choices: ["Use dnd-kit", "Keep inset line"],
                  },
                  can_respond: true,
                  single_active: true,
                }
              : {
                  request_key: options.requestKey,
                  provider_request_id: options.toolCallId,
                  status: "resolved",
                  response_text: options.responseText,
                },
        },
      ],
    },
  });
  expect(
    response.ok(),
    `interaction event failed: ${response.status()} ${await response.text()}`,
  ).toBe(true);
}

async function installWorkspaceFrameProbe(
  page: Page,
  sessionId: string,
): Promise<void> {
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
    globalWindow.EventSource =
      PatchedEventSource as unknown as typeof EventSource;
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
    const requestKey = `e2e:${sessionId}:toolu_ask_user`;

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
    await configureManagedLocalSession(request, sessionId, project);
    await publishInteraction(request, {
      sessionId,
      kind: "pause_request",
      occurredAt: blockedAt,
      requestKey,
      toolCallId: "toolu_ask_user",
    });

    await installWorkspaceFrameProbe(page, sessionId);
    await page.goto(`/timeline/${sessionId}`, {
      waitUntil: "domcontentloaded",
    });
    await page.waitForSelector('body[data-ready="true"]', { timeout: 15_000 });

    const strip = page.getByTestId("session-control-strip");
    await expect(strip).toContainText("Needs answer", { timeout: 10_000 });

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
    await publishInteraction(request, {
      sessionId,
      kind: "pause_resolution",
      occurredAt: answerAt,
      requestKey,
      toolCallId: "toolu_ask_user",
      responseText: "Use dnd-kit",
    });

    await expect
      .poll(async () => {
        const response = await request.get(
          `/api/timeline/sessions/${sessionId}`,
        );
        expect(response.ok(), await response.text()).toBe(true);
        const session = await response.json();
        return session.session_state?.interaction ?? null;
      })
      .toBeNull();

    await expect(
      page
        .getByTestId("session-question-row")
        .filter({ hasText: "Use dnd-kit" })
        .last(),
    ).toBeVisible({ timeout: 5_000 });
    await expect(strip).not.toContainText("Needs answer", { timeout: 5_000 });
    await expect(strip).not.toContainText("original terminal", {
      timeout: 5_000,
    });

    await expect
      .poll(
        async () => {
          return page.evaluate(() => {
            const win = window as unknown as {
              __workspaceFrames__?: unknown[];
            };
            return win.__workspaceFrames__?.length ?? 0;
          });
        },
        { timeout: 5_000 },
      )
      .toBeGreaterThan(0);
  });
});
