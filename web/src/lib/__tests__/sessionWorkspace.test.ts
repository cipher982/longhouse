import { describe, expect, it } from "vitest";
import { buildTimelineModel, getSessionInteractionCapabilities } from "../sessionWorkspace";
import type { AgentSession, AgentSessionProjectionItem, SessionCapabilities } from "../../services/api/agents";

function makeCapabilities(overrides: Partial<SessionCapabilities> = {}): SessionCapabilities {
  return {
    live_control_available: false,
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
    home_label: null,
    branched_from_event_id: null,
    is_writable_head: true,
    control: null,
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
        home_label: "On this Mac",
        control: {
          managed_transport: "codex_app_server",
          source_runner_id: 7,
          source_runner_name: "cinder",
        },
        capabilities: makeCapabilities({
          live_control_available: true,
          host_reattach_available: true,
          reply_to_live_session_available: true,
        }),
      }),
    });

    expect(capabilities.mode).toBe("managed_local");
    expect(capabilities.canChatFromBrowser).toBe(true);
    expect(capabilities.managementLabel).toBe("Managed");
    expect(capabilities.managementDescription).toMatch(/owns the live control path/i);
    expect(capabilities.managedLaunchSuggestion).toBeNull();
    expect(capabilities.capabilityLabel).toBe("Live control");
    expect(capabilities.composerDisabledReason).toBeNull();
    expect(capabilities.primaryActionLabel).toBe("Open live dock");
    expect(capabilities.submitLabel).toBe("Send");
  });

  it("surfaces managed-local sessions without runner metadata as host-reattach only", () => {
    const capabilities = getSessionInteractionCapabilities({
      session: makeSession({
        provider: "codex",
        home_label: "On this Mac",
        control: {
          managed_transport: "codex_app_server",
          source_runner_id: null,
          source_runner_name: null,
        },
        capabilities: makeCapabilities({
          host_reattach_available: true,
        }),
      }),
    });

    expect(capabilities.mode).toBe("managed_local_unavailable");
    expect(capabilities.canChatFromBrowser).toBe(false);
    expect(capabilities.managementLabel).toBe("Managed");
    expect(capabilities.managedLaunchSuggestion).toBeNull();
    expect(capabilities.capabilityLabel).toBe("Reattach on host");
    expect(capabilities.composerDisabledReason).toMatch(/host control channel/i);
    expect(capabilities.primaryActionLabel).toBe("Unavailable");
    expect(capabilities.notice?.title).toMatch(/Codex session needs host attach/i);
  });

  it("shows reattach when a managed-local Claude session loses its live control channel", () => {
    const capabilities = getSessionInteractionCapabilities({
      session: makeSession({
        provider: "claude",
        home_label: "On this Mac",
        control: {
          managed_transport: "claude_channel_bridge",
          source_runner_id: null,
          source_runner_name: null,
        },
        capabilities: makeCapabilities({
          host_reattach_available: true,
        }),
      }),
      isViewingHead: true,
    });

    expect(capabilities.mode).toBe("managed_local_unavailable");
    expect(capabilities.canChatFromBrowser).toBe(false);
    expect(capabilities.managementLabel).toBe("Managed");
    expect(capabilities.managedLaunchSuggestion).toBeNull();
    expect(capabilities.capabilityLabel).toBe("Reattach on host");
  });

  it("treats a synced Claude transcript as search-only", () => {
    const capabilities = getSessionInteractionCapabilities({
      session: makeSession(),
      isViewingHead: true,
    });

    expect(capabilities.mode).toBe("unsupported");
    expect(capabilities.canChatFromBrowser).toBe(false);
    expect(capabilities.managementLabel).toBe("Unmanaged");
    expect(capabilities.capabilityDescription).toMatch(/cannot steer the live session/i);
    expect(capabilities.capabilityDescription).not.toMatch(/longhouse claude/i);
    expect(capabilities.capabilityLabel).toBe("Search only");
    expect(capabilities.primaryActionLabel).toBe("Unavailable");
    expect(capabilities.notice?.title).toBe("Claude session — unmanaged");
    expect(capabilities.managementDescription).toBe("Longhouse imported this Claude session.");
    expect(capabilities.composerDisabledReason).toBe(
      "Live control is unavailable for this unmanaged Claude session.",
    );
    expect(capabilities.managedLaunchSuggestion?.command).toBe("longhouse claude");
  });

  it("treats unsupported providers as searchable context only", () => {
    const capabilities = getSessionInteractionCapabilities({
      session: makeSession({
        provider: "gemini",
        capabilities: makeCapabilities(),
      }),
    });

    expect(capabilities.mode).toBe("unsupported");
    expect(capabilities.canChatFromBrowser).toBe(false);
    expect(capabilities.managementLabel).toBe("Unmanaged");
    expect(capabilities.capabilityLabel).toBe("Search only");
    expect(capabilities.composerDisabledReason).toMatch(/cannot steer the live session/i);
    expect(capabilities.composerDisabledReason).toMatch(/Launch new Gemini sessions through Longhouse/i);
    expect(capabilities.managedLaunchSuggestion).toBeNull();
    expect(capabilities.primaryActionLabel).toBe("Unavailable");
  });
});
