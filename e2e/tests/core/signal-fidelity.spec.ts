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
  test("real engine ingest proves compaction seams, microcompact inclusion, and Opus 1.0M context window in Chromium", async ({
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
        // Positive case: microcompact_boundary ships as a distinct compaction seam.
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
        context_window: 1000000,
        context_window_source: "derived",
      });
      const claudeCompactions = (
        claudeWorkspace.projection.items as Array<Record<string, any>>
      ).filter(
        (item) => item.event?.interaction_kind === "compaction_boundary",
      );
      expect(claudeCompactions).toHaveLength(2);

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
      // 1 microcompact seam + 1 full compaction seam visible + derived 1.0M context ring.
      await page.goto(`/timeline/${claudeSessionId}`, {
        waitUntil: "domcontentloaded",
      });
      const claudeSeams = page.getByTestId("session-compaction-boundary");
      await expect(claudeSeams).toHaveCount(2);
      await expect(claudeSeams.nth(0)).toBeVisible();
      await expect(claudeSeams.nth(0).locator(".tl-seam__label")).toHaveText("Context microcompacted");
      await expect(claudeSeams.nth(0).locator(".tl-seam__description")).toContainText("Auto");
      await expect(claudeSeams.nth(0).locator(".tl-seam__description")).toContainText("42k saved");
      await expect(claudeSeams.nth(1)).toBeVisible();
      await expect(claudeSeams.nth(1).locator(".tl-seam__label")).toHaveText("Context compacted");
      await expect(claudeSeams.nth(1).locator(".tl-seam__description")).toContainText("Manual · 269.6k tokens");
      const claudeSeamBox = await claudeSeams.nth(1).boundingBox();
      expect(claudeSeamBox).not.toBeNull();
      expect(claudeSeamBox!.width).toBeGreaterThan(150);
      expect(claudeSeamBox!.height).toBeGreaterThan(14);
      const claudeUsage = page.getByTestId("session-usage");
      await expect(claudeUsage).toBeVisible();
      await expect(claudeUsage).toContainText("40% of 1.0M");
      const claudeRing = page.getByTestId("session-context-ring");
      await expect(claudeRing).toHaveCount(1);
      await expect(claudeRing).toBeVisible();
      const claudeRingBox = await claudeRing.boundingBox();
      expect(claudeRingBox).not.toBeNull();
      expect(claudeRingBox!.width).toBeGreaterThan(4);
      expect(claudeRingBox!.height).toBeGreaterThan(4);

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

  test("real engine ingest proves Claude 2.1.325 subagents, task notifications, reasoning, commentary, API error, waiting marker, and HUD in Chromium", async ({
    page,
    request,
    backendUrl,
    workerId,
  }) => {
    test.setTimeout(120_000);
    const engineBin = ensureEngineBinary();
    const machineName = `e2e-claude-fidelity-${workerId}-${randomUUID().slice(0, 6)}`;
    const tokenRes = await request.post("/api/devices/tokens", {
      data: { device_id: machineName },
    });
    expect(tokenRes.ok()).toBe(true);
    const { token } = (await tokenRes.json()) as { token: string };

    const tempDir = makeTempEngineDir("longhouse-e2e-claude-fidelity-");
    const claudeSessionId = randomUUID();

    try {
      const parentFile = path.join(tempDir, `${claudeSessionId}.jsonl`);
      const subagentDir = path.join(tempDir, claudeSessionId, "subagents");
      fs.mkdirSync(subagentDir, { recursive: true });
      const subagentFile = path.join(subagentDir, "agent-a1b2c3d.jsonl");
      const subagentMetaFile = path.join(subagentDir, "agent-a1b2c3d.meta.json");

      fs.writeFileSync(
        subagentMetaFile,
        JSON.stringify({
          toolUseId: "toolu_agent_1",
          agentType: "Explore",
          description: "Audit backend signals",
          model: "claude-opus-5",
          isFork: false,
          spawnDepth: 1,
          finished: true,
        }),
        "utf8",
      );

      const subagentLines = [
        {
          type: "user",
          isSidechain: true,
          agentId: "a1b2c3d",
          uuid: "sub-u-1",
          sessionId: claudeSessionId,
          cwd: "/workspace/control-plane",
          gitBranch: "main",
          version: "2.1.325",
          timestamp: "2026-11-14T10:00:05.000Z",
          message: { role: "user", content: "Audit backend signals" },
        },
        {
          type: "assistant",
          isSidechain: true,
          agentId: "a1b2c3d",
          uuid: "sub-a-1",
          parentUuid: "sub-u-1",
          sessionId: claudeSessionId,
          cwd: "/workspace/control-plane",
          gitBranch: "main",
          version: "2.1.325",
          timestamp: "2026-11-14T10:00:20.000Z",
          message: {
            model: "claude-opus-5",
            role: "assistant",
            stop_reason: "end_turn",
            usage: {
              input_tokens: 400,
              cache_creation_input_tokens: 0,
              cache_read_input_tokens: 18000,
              output_tokens: 220,
            },
            content: [{ type: "text", text: "Subagent audit found 0 blockers." }],
          },
        },
      ];
      fs.writeFileSync(
        subagentFile,
        `${subagentLines.map((row) => JSON.stringify(row)).join("\n")}\n`,
        "utf8",
      );

      const parentLines = [
        {
          parentUuid: null,
          isSidechain: false,
          userType: "external",
          cwd: "/workspace/control-plane",
          sessionId: claudeSessionId,
          version: "2.1.325",
          gitBranch: "main",
          type: "user",
          message: { role: "user", content: "Audit Claude 2.1.325 fidelity signals." },
          uuid: "u-1",
          timestamp: "2026-11-14T10:00:00.000Z",
        },
        {
          parentUuid: "u-1",
          isSidechain: false,
          userType: "external",
          cwd: "/workspace/control-plane",
          sessionId: claudeSessionId,
          version: "2.1.325",
          gitBranch: "main",
          type: "assistant",
          message: {
            model: "claude-opus-5",
            id: "msg_01",
            type: "message",
            role: "assistant",
            stop_reason: "tool_use",
            usage: {
              input_tokens: 1200,
              cache_creation_input_tokens: 300,
              cache_read_input_tokens: 45000,
              output_tokens: 85,
            },
            content: [
              { type: "thinking", thinking: "", signature: "ErUBSigTest123" },
              { type: "text", text: "Launching audit subagent and reading config." },
              {
                type: "tool_use",
                id: "toolu_read_1",
                name: "Read",
                input: { file_path: "schemas/managed_providers.yml" },
              },
              {
                type: "tool_use",
                id: "toolu_agent_1",
                name: "Agent",
                input: {
                  description: "Audit backend signals",
                  subagent_type: "Explore",
                },
              },
            ],
          },
          uuid: "a-1",
          timestamp: "2026-11-14T10:00:03.000Z",
        },
        {
          parentUuid: "a-1",
          isSidechain: false,
          userType: "external",
          cwd: "/workspace/control-plane",
          sessionId: claudeSessionId,
          version: "2.1.325",
          gitBranch: "main",
          type: "user",
          message: {
            role: "user",
            content: [
              { type: "tool_result", tool_use_id: "toolu_read_1", content: "ok" },
              { type: "tool_result", tool_use_id: "toolu_agent_1", content: "launched" },
            ],
          },
          toolUseResult: {
            agentId: "a1b2c3d",
            status: "async_launched",
            isAsync: true,
            description: "Audit backend signals",
          },
          uuid: "u-2",
          timestamp: "2026-11-14T10:00:05.000Z",
        },
        {
          parentUuid: "u-2",
          isSidechain: false,
          userType: "external",
          cwd: "/workspace/control-plane",
          sessionId: claudeSessionId,
          version: "2.1.325",
          gitBranch: "main",
          type: "system",
          subtype: "api_error",
          error: "API Error: 529 Overloaded",
          retryAttempt: 1,
          maxRetries: 4,
          retryInMs: 4000,
          level: "error",
          uuid: "s-err",
          timestamp: "2026-11-14T10:00:10.000Z",
        },
        {
          parentUuid: "s-err",
          isSidechain: false,
          userType: "external",
          cwd: "/workspace/control-plane",
          sessionId: claudeSessionId,
          version: "2.1.325",
          gitBranch: "main",
          type: "user",
          origin: { kind: "task-notification" },
          message: {
            role: "user",
            content:
              '<task-notification>\n<task-id>a1b2c3d</task-id>\n<tool-use-id>toolu_agent_1</tool-use-id>\n<status>completed</status>\n<summary>Agent "Audit backend signals" completed</summary>\n<result>Found 0 blockers</result>\n<usage><total_tokens>24500</total_tokens><tool_uses>7</tool_uses><duration_ms>18320</duration_ms></usage>\n</task-notification>',
          },
          uuid: "u-notif",
          timestamp: "2026-11-14T10:00:22.000Z",
        },
        {
          parentUuid: "u-notif",
          isSidechain: false,
          userType: "external",
          cwd: "/workspace/control-plane",
          sessionId: claudeSessionId,
          version: "2.1.325",
          gitBranch: "main",
          type: "assistant",
          message: {
            model: "claude-opus-5",
            id: "msg_02",
            type: "message",
            role: "assistant",
            stop_reason: "end_turn",
            usage: {
              input_tokens: 200,
              cache_creation_input_tokens: 1000,
              cache_read_input_tokens: 130000,
              output_tokens: 240,
            },
            content: [
              { type: "thinking", thinking: "Synthesizing subagent findings cleanly." },
              { type: "text", text: "Audit complete with 0 blockers." },
            ],
          },
          uuid: "a-2",
          timestamp: "2026-11-14T10:00:25.000Z",
        },
        {
          parentUuid: "a-2",
          isSidechain: false,
          userType: "external",
          cwd: "/workspace/control-plane",
          sessionId: claudeSessionId,
          version: "2.1.325",
          gitBranch: "main",
          type: "system",
          subtype: "turn_duration",
          durationMs: 18000,
          messageCount: 6,
          pendingBackgroundAgentCount: 2,
          uuid: "s-td",
          timestamp: "2026-11-14T10:00:26.000Z",
        },
        {
          parentUuid: "s-td",
          isSidechain: false,
          userType: "external",
          cwd: "/workspace/control-plane",
          sessionId: claudeSessionId,
          version: "2.1.325",
          gitBranch: "main",
          type: "system",
          subtype: "scheduled_task_fire",
          taskId: "cron-1",
          taskKind: "cron",
          cron: "*/15 * * * *",
          cronKind: "recurring",
          prompt: "Check canary",
          content: "Running scheduled canary check",
          uuid: "s-cron",
          timestamp: "2026-11-14T10:00:28.000Z",
        },
        {
          parentUuid: "s-cron",
          isSidechain: false,
          userType: "external",
          cwd: "/workspace/control-plane",
          sessionId: claudeSessionId,
          version: "2.1.325",
          gitBranch: "main",
          type: "system",
          subtype: "away_summary",
          content: "Landed Step 2 projections.",
          uuid: "s-away",
          timestamp: "2026-11-14T10:00:30.000Z",
        },
        {
          type: "cost-state",
          sessionId: claudeSessionId,
          totalCostUSD: 53.89,
          totalDuration: 28860000,
        },
        {
          type: "permission-mode",
          sessionId: claudeSessionId,
          permissionMode: "bypassPermissions",
        },
        {
          type: "worktree-state",
          sessionId: claudeSessionId,
          worktreeSession: {
            worktreePath: "/workspace/.claude/worktrees/claude-fidelity",
            worktreeBranch: "claude-fidelity",
            worktreeName: "claude-fidelity",
            originalCwd: "/workspace/control-plane",
          },
        },
      ];
      fs.writeFileSync(
        parentFile,
        `${parentLines.map((row) => JSON.stringify(row)).join("\n")}\n`,
        "utf8",
      );

      const engineDbPath = path.join(tempDir, "engine-claude-fidelity.db");
      await withWorkerHeaderProxy(backendUrl, workerId, async (proxyUrl) => {
        await shipTranscriptFile({
          engineBin,
          filePath: parentFile,
          backendUrl: proxyUrl,
          token,
          provider: "claude",
          machineName,
          engineDbPath,
        });
        await shipTranscriptFile({
          engineBin,
          filePath: subagentFile,
          backendUrl: proxyUrl,
          token,
          provider: "claude",
          machineName,
          engineDbPath,
        });
      });

      const workspace = await waitForWorkspaceSession(request, claudeSessionId);
      expect(workspace.session.usage_latest).toMatchObject({
        context_tokens: 131200,
        context_window: 1000000,
        context_window_source: "derived",
        cost_usd: 53.89,
      });

      await page.goto(`/timeline/${claudeSessionId}`, {
        waitUntil: "domcontentloaded",
      });

      await expect(page.getByTestId("session-usage")).toContainText("13% of 1.0M");
      await expect(page.getByTestId("session-cost")).toContainText("$53.89");
      await expect(page.getByTestId("session-cumulative-duration")).toContainText("8h 1m");
      await expect(page.getByTestId("session-git-context")).toContainText("git:(claude-fidelity) wt:claude-fidelity");
      await expect(page.getByTestId("session-permission-mode")).toContainText("bypass permissions");
      await expect(page.getByTestId("session-crons-pill")).toContainText("1 cron");
      await expect(page.getByTestId("session-away-recap")).toContainText("Landed Step 2 projections.");

      await expect(page.getByTestId("session-reasoning-withheld")).toContainText("Reasoning withheld");
      await expect(page.getByTestId("session-message-phase-commentary")).toBeVisible();

      const subagentWidget = page.getByTestId("tool-subagents");
      await expect(subagentWidget).toBeVisible();
      await subagentWidget.locator("summary").click();
      await expect(subagentWidget).toContainText("Audit backend signals");

      const taskNotif = page.getByTestId("session-task-notification");
      await expect(taskNotif).toBeVisible();
      await expect(taskNotif).toContainText("18s");
      await expect(taskNotif).toContainText("24.5k tokens");
      await expect(taskNotif).toContainText("7 tool uses");
      await taskNotif.getByRole("button").click();
      await expect(page.getByTestId("session-task-notification-result")).toContainText("Found 0 blockers");

      await expect(page.getByTestId("session-waiting-background-agents")).toContainText(
        "Waiting for 2 background agents to finish",
      );
      await expect(page.getByTestId("session-api-error-notice")).toContainText("retry 1/4 · in 4s");
      await expect(page.getByTestId("session-turn-end")).toContainText("18s");
    } finally {
      fs.rmSync(tempDir, { recursive: true, force: true });
    }
  });
});
