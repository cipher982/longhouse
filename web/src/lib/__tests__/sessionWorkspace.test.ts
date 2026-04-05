import { describe, expect, it } from "vitest";
import { buildTimelineModel, getSessionInteractionCapabilities } from "../sessionWorkspace";
import type { AgentSession, AgentSessionProjectionItem, SessionCapabilities } from "../../services/api/agents";

function makeCapabilities(overrides: Partial<SessionCapabilities> = {}): SessionCapabilities {
  return {
    live_control_available: false,
    cloud_branch_available: true,
    host_reattach_available: false,
    reply_to_live_session_available: false,
    ...overrides,
  };
}

function makeSession(overrides: Partial<AgentSession> = {}): AgentSession {
  return {
    id: "session-1",
    provider: "claude",
    project: "zerg",
    device_id: "cinder",
    environment: "development",
    cwd: "/Users/davidrose/git/zerg",
    git_repo: "git@github.com:cipher982/longhouse.git",
    git_branch: "main",
    started_at: "2026-03-22T22:00:00Z",
    ended_at: "2026-03-22T22:05:00Z",
    last_activity_at: "2026-03-22T22:05:00Z",
    user_messages: 1,
    assistant_messages: 1,
    tool_calls: 1,
    summary: "Investigated session controls",
    summary_title: "Session controls",
    first_user_message: "Verify the session controls",
    thread_root_session_id: "session-1",
    thread_head_session_id: "session-1",
    thread_continuation_count: 1,
    continued_from_session_id: null,
    continuation_kind: "local",
    origin_label: "On this Mac",
    execution_home: "legacy",
    branched_from_event_id: null,
    is_writable_head: true,
    managed_transport: null,
    source_runner_id: null,
    source_runner_name: null,
    attach_command: null,
    capabilities: makeCapabilities(),
    loop_mode: "manual",
    ...overrides,
  };
}

describe("buildTimelineModel", () => {
  it("preserves the reported tool name for orphan tool results", () => {
    const items: AgentSessionProjectionItem[] = [
      {
        kind: "event",
        session_id: "session-codex",
        timestamp: "2026-03-22T22:00:00Z",
        event: {
          id: 42,
          role: "tool",
          content_text: null,
          tool_name: "Bash",
          tool_input_json: null,
          tool_output_text: "README.md",
          tool_call_id: null,
          timestamp: "2026-03-22T22:00:00Z",
          in_active_context: true,
        },
      },
    ];

    const model = buildTimelineModel(items);
    expect(model.items).toHaveLength(1);

    const [toolItem] = model.items;
    expect(toolItem?.kind).toBe("tool");
    if (!toolItem || toolItem.kind !== "tool") {
      throw new Error("Expected a tool timeline item");
    }

    expect(toolItem.interaction.toolName).toBe("Bash");
    const selection = model.selectionMap.get("tool:orphan:42");
    expect(selection?.kind).toBe("tool");
    if (!selection || selection.kind !== "tool") {
      throw new Error("Expected an orphan tool selection");
    }
    expect(selection.interaction.toolName).toBe("Bash");
  });
});

describe("getSessionInteractionCapabilities", () => {
  it("treats managed-local sessions with runner metadata as browser-drivable live sessions", () => {
    const capabilities = getSessionInteractionCapabilities({
      session: makeSession({
        provider: "codex",
        execution_home: "managed_local",
        managed_transport: "codex_app_server",
        source_runner_id: 7,
        source_runner_name: "cinder",
        capabilities: makeCapabilities({
          live_control_available: true,
          cloud_branch_available: false,
          host_reattach_available: true,
          reply_to_live_session_available: true,
        }),
      }),
    });

    expect(capabilities.mode).toBe("managed_local");
    expect(capabilities.canChatFromBrowser).toBe(true);
    expect(capabilities.capabilityLabel).toBe("Live control");
    expect(capabilities.composerDisabledReason).toBeNull();
    expect(capabilities.primaryActionLabel).toBe("Open live dock");
    expect(capabilities.submitLabel).toBe("Send");
  });

  it("surfaces managed-local sessions without runner metadata as host-reattach only", () => {
    const capabilities = getSessionInteractionCapabilities({
      session: makeSession({
        provider: "codex",
        execution_home: "managed_local",
        managed_transport: "codex_app_server",
        capabilities: makeCapabilities({
          cloud_branch_available: false,
          host_reattach_available: true,
        }),
      }),
    });

    expect(capabilities.mode).toBe("managed_local_unavailable");
    expect(capabilities.canChatFromBrowser).toBe(false);
    expect(capabilities.capabilityLabel).toBe("Reattach on host");
    expect(capabilities.composerDisabledReason).toMatch(/host control channel/i);
    expect(capabilities.primaryActionLabel).toBe("Unavailable");
    expect(capabilities.notice?.title).toMatch(/Codex session needs host attach/i);
  });

  it("lets managed-local Claude sessions start a browser cloud branch when the live control channel is gone", () => {
    const capabilities = getSessionInteractionCapabilities({
      session: makeSession({
        provider: "claude",
        execution_home: "managed_local",
        managed_transport: "tmux",
        source_runner_id: null,
        source_runner_name: null,
        capabilities: makeCapabilities({
          cloud_branch_available: true,
          host_reattach_available: true,
        }),
      }),
      isViewingHead: true,
    });

    expect(capabilities.mode).toBe("promote");
    expect(capabilities.canChatFromBrowser).toBe(true);
    expect(capabilities.capabilityLabel).toBe("Cloud branch");
    expect(capabilities.capabilitySummary).toBe("Start a cloud branch from this session.");
    expect(capabilities.title).toBe("Start Cloud Branch");
    expect(capabilities.description).toContain("starts a new cloud branch from this session in Longhouse");
    expect(capabilities.submitLabel).toBe("Start Cloud Branch");
    expect(capabilities.composerDisabledReason).toBeNull();
    expect(capabilities.primaryActionLabel).toBe("Open branch dock");
  });

  it("treats a synced Claude transcript on the head as promotable to a cloud branch", () => {
    const capabilities = getSessionInteractionCapabilities({
      session: makeSession(),
      isViewingHead: true,
    });

    expect(capabilities.mode).toBe("promote");
    expect(capabilities.canChatFromBrowser).toBe(true);
    expect(capabilities.capabilityLabel).toBe("Cloud branch");
    expect(capabilities.capabilitySummary).toBe("Start a cloud branch from this session.");
    expect(capabilities.title).toBe("Start Cloud Branch");
    expect(capabilities.submitLabel).toBe("Start Cloud Branch");
    expect(capabilities.primaryActionLabel).toBe("Open branch dock");
  });

  it("treats unsupported providers as searchable context only", () => {
    const capabilities = getSessionInteractionCapabilities({
      session: makeSession({
        provider: "gemini",
        capabilities: makeCapabilities({
          cloud_branch_available: false,
        }),
      }),
    });

    expect(capabilities.mode).toBe("unsupported");
    expect(capabilities.canChatFromBrowser).toBe(false);
    expect(capabilities.capabilityLabel).toBe("History only");
    expect(capabilities.composerDisabledReason).toMatch(/cloud branching is not available/i);
    expect(capabilities.primaryActionLabel).toBe("Unavailable");
  });
});
