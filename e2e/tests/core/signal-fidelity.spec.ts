import { randomUUID } from "crypto";
import fs from "fs";
import path from "path";
import type { APIRequestContext } from "@playwright/test";
import { test, expect } from "../fixtures";
import {
  ensureEngineBinary,
  makeTempEngineDir,
  shipTranscriptFile,
  withWorkerHeaderProxy,
} from "../engine-fixtures";

async function waitForWorkspaceSession(
  request: APIRequestContext,
  sessionId: string,
): Promise<Record<string, any>> {
  let latestBody: Record<string, any> | null = null;
  await expect
    .poll(
      async () => {
        const response = await request.get(
          `/api/timeline/sessions/${sessionId}/workspace?limit=50`,
        );
        if (!response.ok()) return false;
        const body = (await response.json()) as Record<string, any>;
        latestBody = body;
        return (
          body.session?.id === sessionId &&
          Array.isArray(body.projection?.items)
        );
      },
      { timeout: 30_000 },
    )
    .toBe(true);
  if (!latestBody) {
    throw new Error(`Workspace did not expose session ${sessionId}`);
  }
  return latestBody;
}

test.describe("0-to-1 engine-to-pixel signal fidelity", () => {
  test("real engine ingest proves compaction seams, microcompact exclusion, and context-window honesty in Chromium", async ({
    page,
    request,
    backendUrl,
    workerId,
  }) => {
    test.setTimeout(120_000);
    const engineBin = ensureEngineBinary();
    const machineName = `e2e-signal-${workerId}-${randomUUID().slice(0, 6)}`;
    const tokenRes = await request.post("/api/devices/tokens", {
      data: { device_id: machineName },
    });
    expect(tokenRes.ok()).toBe(true);
    const { token } = (await tokenRes.json()) as { token: string };

    const tempDir = makeTempEngineDir("longhouse-e2e-signal-");
    const claudeSessionId = randomUUID();
    const codexSessionId = randomUUID();

    try {
      const claudeFile = path.join(tempDir, `${claudeSessionId}.jsonl`);
      const claudeLines = [
        {
          parentUuid: null,
          isSidechain: false,
          userType: "external",
          cwd: "/workspace/audit",
          sessionId: claudeSessionId,
          version: "2.1.258",
          gitBranch: "main",
          type: "user",
          message: { role: "user", content: "Verify Claude compaction fidelity" },
          uuid: "u-1",
          timestamp: "2026-10-01T10:00:00.000Z",
        },
        {
          parentUuid: "u-1",
          isSidechain: false,
          userType: "external",
          cwd: "/workspace/audit",
          sessionId: claudeSessionId,
          version: "2.1.258",
          gitBranch: "main",
          type: "assistant",
          message: {
            model: "claude-opus-5",
            id: "msg_01",
            type: "message",
            role: "assistant",
            content: [{ type: "text", text: "Running checks before compaction." }],
            stop_reason: "end_turn",
            usage: {
              input_tokens: 2,
              cache_creation_input_tokens: 300,
              cache_read_input_tokens: 401000,
              output_tokens: 177,
            },
          },
          uuid: "a-1",
          timestamp: "2026-10-01T10:00:05.000Z",
        },
        // Negative case: microcompact_boundary must stay hidden and never render a compaction divider.
        {
          parentUuid: "a-1",
          isSidechain: false,
          userType: "external",
          cwd: "/workspace/audit",
          sessionId: claudeSessionId,
          version: "2.1.258",
          gitBranch: "main",
          type: "system",
          subtype: "microcompact_boundary",
          content: "Microcompacted tool outputs",
          microcompactMetadata: {
            trigger: "auto",
            preTokens: 280000,
            tokensSaved: 42000,
          },
          uuid: "s-micro",
          timestamp: "2026-10-01T10:00:10.000Z",
        },
        // Positive case: full compact_boundary must ship and render one compaction seam bar.
        {
          parentUuid: "s-micro",
          isSidechain: false,
          userType: "external",
          cwd: "/workspace/audit",
          sessionId: claudeSessionId,
          version: "2.1.258",
          gitBranch: "main",
          type: "system",
          subtype: "compact_boundary",
          content: "Conversation compacted",
          compactMetadata: {
            trigger: "manual",
            preTokens: 269584,
            postTokens: 15694,
            cumulativeDroppedTokens: 253890,
            durationMs: 144947,
          },
          uuid: "s-compact",
          timestamp: "2026-10-01T10:01:00.000Z",
        },
        {
          parentUuid: "s-compact",
          isSidechain: false,
          userType: "external",
          cwd: "/workspace/audit",
          sessionId: claudeSessionId,
          version: "2.1.258",
          gitBranch: "main",
          type: "assistant",
          message: {
            model: "claude-opus-5",
            id: "msg_02",
            type: "message",
            role: "assistant",
            content: [{ type: "text", text: "Resumed after compaction." }],
            stop_reason: "end_turn",
            usage: {
              input_tokens: 2,
              cache_creation_input_tokens: 300,
              cache_read_input_tokens: 401000,
              output_tokens: 90,
            },
          },
          uuid: "a-2",
          timestamp: "2026-10-01T10:01:10.000Z",
        },
      ];
      fs.writeFileSync(
        claudeFile,
        `${claudeLines.map((row) => JSON.stringify(row)).join("\n")}\n`,
        "utf8",
      );

      const codexFile = path.join(tempDir, `rollout-${codexSessionId}.jsonl`);
      const codexLines = [
        {
          timestamp: "2026-10-01T10:05:00.000Z",
          ordinal: 0,
          type: "session_meta",
          payload: {
            session_id: codexSessionId,
            id: codexSessionId,
            timestamp: "2026-10-01T10:05:00.000Z",
            cwd: "/workspace/audit",
            originator: "codex_cli_rs",
            cli_version: "0.151.0",
            source: "cli",
            model_provider: "openai",
          },
        },
        {
          timestamp: "2026-10-01T10:05:00.100Z",
          ordinal: 1,
          type: "event_msg",
          payload: {
            type: "task_started",
            turn_id: "turn-codex-1",
            started_at: 1790849100,
            model_context_window: 258400,
            collaboration_mode_kind: "default",
          },
        },
        {
          timestamp: "2026-10-01T10:05:00.200Z",
          ordinal: 2,
          type: "turn_context",
          payload: {
            turn_id: "turn-codex-1",
            cwd: "/workspace/audit",
            model: "gpt-5.6-luna",
          },
        },
        {
          timestamp: "2026-10-01T10:05:00.300Z",
          ordinal: 3,
          type: "response_item",
          payload: {
            type: "message",
            role: "user",
            content: [{ type: "input_text", text: "Verify Codex compaction and context." }],
          },
        },
        {
          timestamp: "2026-10-01T10:05:02.000Z",
          ordinal: 4,
          type: "event_msg",
          payload: {
            type: "token_count",
            info: {
              total_token_usage: {
                input_tokens: 24755,
                cached_input_tokens: 24700,
                cache_write_input_tokens: 300,
                output_tokens: 130,
                reasoning_output_tokens: 50,
                total_tokens: 24885,
              },
              last_token_usage: {
                input_tokens: 24755,
                cached_input_tokens: 24700,
                cache_write_input_tokens: 300,
                output_tokens: 130,
                reasoning_output_tokens: 50,
                total_tokens: 24885,
              },
              model_context_window: 258400,
            },
            rate_limits: null,
          },
        },
        {
          timestamp: "2026-10-01T10:05:04.000Z",
          ordinal: 5,
          type: "response_item",
          payload: {
            type: "message",
            role: "assistant",
            content: [{ type: "output_text", text: "Codex reply before compaction." }],
          },
        },
        {
          timestamp: "2026-10-01T10:05:10.000Z",
          ordinal: 6,
          type: "compacted",
          payload: {
            message: "",
            replacement_history: [
              {
                type: "message",
                id: "msg_1",
                role: "user",
                content: [{ type: "input_text", text: "Verify Codex compaction and context." }],
              },
              {
                type: "message",
                id: "msg_2",
                role: "assistant",
                content: [{ type: "output_text", text: "Audit rolled up cleanly." }],
              },
            ],
          },
        },
        {
          timestamp: "2026-10-01T10:05:12.000Z",
          ordinal: 7,
          type: "event_msg",
          payload: {
            type: "task_complete",
            turn_id: "turn-codex-1",
            last_agent_message: "Codex post-compaction state ready.",
          },
        },
      ];
      fs.writeFileSync(
        codexFile,
        `${codexLines.map((row) => JSON.stringify(row)).join("\n")}\n`,
        "utf8",
      );

      await withWorkerHeaderProxy(backendUrl, workerId, async (proxyUrl) => {
        await shipTranscriptFile({
          engineBin,
          filePath: claudeFile,
          backendUrl: proxyUrl,
          token,
          provider: "claude",
          machineName,
          engineDbPath: path.join(tempDir, "engine-claude.db"),
        });
        await shipTranscriptFile({
          engineBin,
          filePath: codexFile,
          backendUrl: proxyUrl,
          token,
          provider: "codex",
          machineName,
          engineDbPath: path.join(tempDir, "engine-codex.db"),
        });
      });

      // Assert served workspace contract directly from Rust engine -> Storage v2 -> API.
      const claudeWorkspace = await waitForWorkspaceSession(request, claudeSessionId);
      expect(claudeWorkspace.session.usage_latest).toMatchObject({
        context_tokens: 401302,
        context_window: null,
        context_window_source: null,
      });
      const claudeCompactions = (
        claudeWorkspace.projection.items as Array<Record<string, any>>
      ).filter(
        (item) => item.event?.interaction_kind === "compaction_boundary",
      );
      expect(claudeCompactions).toHaveLength(1);

      const codexWorkspace = await waitForWorkspaceSession(request, codexSessionId);
      expect(codexWorkspace.session.usage_latest).toMatchObject({
        context_window: 258400,
        context_window_source: "native",
      });
      const codexCompactions = (
        codexWorkspace.projection.items as Array<Record<string, any>>
      ).filter(
        (item) => item.event?.interaction_kind === "compaction_boundary",
      );
      expect(codexCompactions).toHaveLength(1);

      // Assert rendered Chromium DOM & pixel geometry for Claude session:
      // 1 full compaction divider visible, microcompact absent, no fabricated context ring.
      await page.goto(`/timeline/${claudeSessionId}`, {
        waitUntil: "domcontentloaded",
      });
      const claudeSeam = page.getByTestId("session-compaction-boundary");
      await expect(claudeSeam).toHaveCount(1);
      await expect(claudeSeam).toBeVisible();
      await expect(claudeSeam.locator(".tl-seam__label")).toHaveText("Context compacted");
      await expect(claudeSeam.locator(".tl-seam__description")).toContainText("Manual · 269.6k tokens");
      const claudeSeamBox = await claudeSeam.boundingBox();
      expect(claudeSeamBox).not.toBeNull();
      expect(claudeSeamBox!.width).toBeGreaterThan(150);
      expect(claudeSeamBox!.height).toBeGreaterThan(14);
      const claudeUsage = page.getByTestId("session-usage");
      await expect(claudeUsage).toBeVisible();
      await expect(claudeUsage).toContainText("401k ctx");
      await expect(claudeUsage).not.toContainText("/");
      await expect(page.getByTestId("session-context-ring")).toHaveCount(0);

      // Assert rendered Chromium DOM & pixel geometry for Codex session:
      // 1 Codex compaction divider visible + native /258k ctx + rendered context ring.
      await page.goto(`/timeline/${codexSessionId}`, {
        waitUntil: "domcontentloaded",
      });
      const codexSeam = page.getByTestId("session-compaction-boundary");
      await expect(codexSeam).toHaveCount(1);
      await expect(codexSeam).toBeVisible();
      const codexSeamBox = await codexSeam.boundingBox();
      expect(codexSeamBox).not.toBeNull();
      expect(codexSeamBox!.width).toBeGreaterThan(150);
      expect(codexSeamBox!.height).toBeGreaterThan(14);
      const codexUsage = page.getByTestId("session-usage");
      await expect(codexUsage).toBeVisible();
      await expect(codexUsage).toContainText("/258k ctx");
      const codexRing = page.getByTestId("session-context-ring");
      await expect(codexRing).toHaveCount(1);
      await expect(codexRing).toBeVisible();
      const codexRingBox = await codexRing.boundingBox();
      expect(codexRingBox).not.toBeNull();
      expect(codexRingBox!.width).toBeGreaterThan(4);
      expect(codexRingBox!.height).toBeGreaterThan(4);
    } finally {
      fs.rmSync(tempDir, { recursive: true, force: true });
    }
  });
});
