import { describe, expect, it } from "vitest";
import {
  buildTimelineModel,
  getSessionInteractionCapabilities,
  getTimelineMessagePreview,
  isToolInteractionDropped,
  projectionItemsWithTranscriptPreview,
} from "../sessionWorkspace";
import type { ToolInteraction } from "../sessionWorkspace";
import type { AgentEvent, AgentSession, AgentSessionProjectionItem, SessionCapabilities } from "../../services/api/agents";

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
    loop_mode: "assist",
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

describe("projectionItemsWithTranscriptPreview", () => {
  const baseEvent: AgentEvent = {
    id: 1,
    role: "user",
    content_text: "Prompt",
    tool_name: null,
    tool_input_json: null,
    tool_output_text: null,
    tool_call_id: null,
    timestamp: "2026-03-22T22:00:00Z",
    in_active_context: true,
  };

  it("appends a fresh provisional preview as a synthetic assistant event", () => {
    const session = makeSession({
      transcript_preview: {
        event_id: 42,
        text: "Partial live answer",
        event_origin: "live_provisional",
        timestamp: "2026-03-22T22:00:05Z",
        is_provisional: true,
        is_complete: false,
        content_cursor: "cursor-1",
        is_stale: false,
        stale_reason: null,
      },
    });
    const items: AgentSessionProjectionItem[] = [
      {
        kind: "event",
        session_id: session.id,
        timestamp: baseEvent.timestamp,
        event: baseEvent,
      },
    ];

    const withPreview = projectionItemsWithTranscriptPreview(items, session);

    expect(withPreview).toHaveLength(2);
    expect(withPreview[1]?.event).toMatchObject({
      id: -42,
      role: "assistant",
      content_text: "Partial live answer",
      timestamp: "2026-03-22T22:00:05Z",
    });
  });

  it("skips previews already superseded by durable transcript events", () => {
    const session = makeSession({
      transcript_preview: {
        event_id: 42,
        text: "Partial live answer",
        event_origin: "live_provisional",
        timestamp: "2026-03-22T22:00:05Z",
        is_provisional: true,
        is_complete: true,
        content_cursor: "cursor-1",
        is_stale: false,
        stale_reason: null,
      },
    });
    const items: AgentSessionProjectionItem[] = [
      {
        kind: "event",
        session_id: session.id,
        timestamp: baseEvent.timestamp,
        event: baseEvent,
      },
      {
        kind: "event",
        session_id: session.id,
        timestamp: "2026-03-22T22:00:06Z",
        event: {
          ...baseEvent,
          id: 2,
          role: "assistant",
          content_text: "Partial live answer",
          timestamp: "2026-03-22T22:00:06Z",
        },
      },
    ];

    expect(projectionItemsWithTranscriptPreview(items, session)).toBe(items);
  });
});

describe("getTimelineMessagePreview", () => {
  it("trusts server-projected display text instead of stripping provider wrappers locally", () => {
    const event: AgentEvent = {
      id: 7,
      role: "user",
      content_text: "<channel name=\"commentary\">\nkeep raw if server sent raw\n</channel>",
      tool_name: null,
      tool_input_json: null,
      tool_output_text: null,
      tool_call_id: null,
      timestamp: "2026-03-22T22:00:00Z",
      in_active_context: true,
    };

    expect(getTimelineMessagePreview(event)).toBe(
      "<channel name=\"commentary\">\nkeep raw if server sent raw\n</channel>",
    );
  });
});

describe("isToolInteractionDropped", () => {
  const baseCall: AgentEvent = {
    id: 1,
    role: "assistant",
    content_text: null,
    tool_name: "Bash",
    tool_input_json: null,
    tool_output_text: null,
    tool_call_id: "tc-1",
    timestamp: "2026-03-22T22:00:00Z",
    in_active_context: true,
  };

  function makeInteraction(overrides: Partial<ToolInteraction> = {}): ToolInteraction {
    return {
      key: "id:tc-1",
      toolName: "Bash",
      callEvent: baseCall,
      resultEvent: null,
      pairing: "id",
      anchorId: 1,
      timestamp: baseCall.timestamp,
      ...overrides,
    };
  }

  const nowOlder = Date.parse("2026-03-22T23:30:00Z"); // 90min after call
  const nowRecent = Date.parse("2026-03-22T22:10:00Z"); // 10min after call

  it("returns false when a result is present", () => {
    const interaction = makeInteraction({ resultEvent: { ...baseCall, id: 2, role: "tool" } });
    expect(isToolInteractionDropped(interaction, false, nowOlder)).toBe(false);
  });

  it("marks an unresolved call as dropped once the session has ended", () => {
    expect(isToolInteractionDropped(makeInteraction(), true, nowRecent)).toBe(true);
  });

  it("marks an unresolved call older than 1 hour as dropped in a live session", () => {
    expect(isToolInteractionDropped(makeInteraction(), false, nowOlder)).toBe(true);
  });

  it("leaves fresh unresolved calls in a live session as pending (not dropped)", () => {
    expect(isToolInteractionDropped(makeInteraction(), false, nowRecent)).toBe(false);
  });

  it("never marks orphan results as dropped", () => {
    const interaction = makeInteraction({ pairing: "orphan" });
    expect(isToolInteractionDropped(interaction, true, nowOlder)).toBe(false);
  });
});

describe("getSessionInteractionCapabilities", () => {
  it("treats managed-local sessions with runner metadata as browser-drivable live sessions", () => {
    const capabilities = getSessionInteractionCapabilities({
      session: makeSession({
        provider: "codex",
        home_label: "On this Mac",
        control: {
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
    expect(capabilities.managementDescription).toMatch(/owns the control path/i);
    expect(capabilities.managedLaunchSuggestion).toBeNull();
    expect(capabilities.capabilityLabel).toBe("Send");
    expect(capabilities.composerDisabledReason).toBeNull();
    expect(capabilities.sendDisabledReason).toBeNull();
    expect(capabilities.primaryActionLabel).toBe("Open live dock");
    expect(capabilities.submitLabel).toBe("Send");
  });

  it("uses runtime display control_path as the ownership axis", () => {
    const capabilities = getSessionInteractionCapabilities({
      session: makeSession({
        provider: "codex",
        runtime_display: {
          truth_tier: "fresh",
          state: "idle",
          tone: "idle",
          headline: "Idle",
          detail: "Waiting",
          phase_label: "Idle",
          compact_tool_label: null,
          is_live: false,
          is_executing: false,
          needs_attention: false,
          is_idle: true,
          is_managed_local_truth: true,
          has_signal: true,
          control_path: "managed",
          lifecycle: "open",
          activity_recency: "stale",
          host_state: "offline",
          terminal_reason: null,
        },
        capabilities: makeCapabilities(),
      }),
    });

    expect(capabilities.mode).toBe("unsupported");
    expect(capabilities.managementLabel).toBe("Managed");
    expect(capabilities.managedLaunchSuggestion).toBeNull();
    expect(capabilities.capabilityLabel).toBe("Read only");
    expect(capabilities.composerDisabledReason).toMatch(/managed Codex session is read-only/i);
  });

  it("keeps managed Antigravity sessions observe-only when agy exposes no send lane", () => {
    const capabilities = getSessionInteractionCapabilities({
      session: makeSession({
        provider: "antigravity",
        runtime_display: {
          truth_tier: "fresh",
          state: "idle",
          tone: "idle",
          headline: "Idle",
          detail: "Waiting",
          phase_label: "Idle",
          compact_tool_label: null,
          is_live: false,
          is_executing: false,
          needs_attention: false,
          is_idle: true,
          is_managed_local_truth: true,
          has_signal: true,
          control_path: "managed",
          lifecycle: "open",
          activity_recency: "recent",
          host_state: "online",
          terminal_reason: null,
        },
        capabilities: makeCapabilities({
          input_mode: "read_only",
          composer_enabled: false,
          can_send_input: false,
          can_interrupt: false,
          can_resume: false,
        }),
      }),
    });

    expect(capabilities.mode).toBe("unsupported");
    expect(capabilities.canChatFromBrowser).toBe(false);
    expect(capabilities.managementLabel).toBe("Managed");
    expect(capabilities.managedLaunchSuggestion).toBeNull();
    expect(capabilities.capabilityLabel).toBe("Read only");
    expect(capabilities.composerDisabledReason).toMatch(/managed Antigravity session is read-only/i);
    expect(capabilities.primaryActionLabel).toBe("Unavailable");
  });

  it("prefers server read-only input mode over host reattach fallback", () => {
    const capabilities = getSessionInteractionCapabilities({
      session: makeSession({
        provider: "codex",
        capabilities: makeCapabilities({
          live_control_available: true,
          host_reattach_available: true,
          reply_to_live_session_available: false,
          input_mode: "read_only",
          composer_enabled: false,
          composer_disabled_reason: "This live Codex session is connected, but this control path cannot accept typed input.",
          send_disabled_reason: "input_not_supported",
        }),
      }),
    });

    expect(capabilities.mode).toBe("unsupported");
    expect(capabilities.managementLabel).toBe("Managed");
    expect(capabilities.sendDisabledReason).toBe("input_not_supported");
    expect(capabilities.composerDisabledReason).toBe(
      "This live Codex session is connected, but this control path cannot accept typed input.",
    );
    expect(capabilities.composerDisabledReason).not.toMatch(/engine reconnects/i);
  });

  it("surfaces managed-local sessions without runner metadata as host-reattach only", () => {
    const capabilities = getSessionInteractionCapabilities({
      session: makeSession({
        provider: "codex",
        home_label: "On this Mac",
        control: {
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
    expect(capabilities.capabilityLabel).toBe("Control offline");
    expect(capabilities.composerDisabledReason).toMatch(/cannot send prompts/i);
    expect(capabilities.composerDisabledReason).toMatch(/engine reconnects/i);
    expect(capabilities.primaryActionLabel).toBe("Unavailable");
    expect(capabilities.notice?.title).toBe("Control is offline");
  });

  it("prefers server-owned composer semantics when present", () => {
    const capabilities = getSessionInteractionCapabilities({
      session: makeSession({
        provider: "codex",
        capabilities: makeCapabilities({
          live_control_available: true,
          host_reattach_available: true,
          reply_to_live_session_available: true,
          input_mode: "offline",
          composer_placeholder: "Server placeholder",
          composer_disabled_reason: "Server says control is offline.",
          send_disabled_reason: "control_offline",
        }),
      }),
    });

    expect(capabilities.mode).toBe("managed_local_unavailable");
    expect(capabilities.placeholder).toBe("Server placeholder");
    expect(capabilities.composerDisabledReason).toBe("Server says control is offline.");
    expect(capabilities.sendDisabledReason).toBe("control_offline");
  });

  it("shows reattach when a managed-local Claude session loses its live control channel", () => {
    const capabilities = getSessionInteractionCapabilities({
      session: makeSession({
        provider: "claude",
        home_label: "On this Mac",
        control: {
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
    expect(capabilities.capabilityLabel).toBe("Control offline");
  });

  it("treats a synced Claude transcript as search-only", () => {
    const capabilities = getSessionInteractionCapabilities({
      session: makeSession(),
      isViewingHead: true,
    });

    expect(capabilities.mode).toBe("unsupported");
    expect(capabilities.canChatFromBrowser).toBe(false);
    expect(capabilities.managementLabel).toBe("Unmanaged");
    expect(capabilities.capabilityDescription).toMatch(/cannot steer it/i);
    expect(capabilities.capabilityDescription).not.toMatch(/longhouse claude/i);
    expect(capabilities.capabilityLabel).toBe("Read only");
    expect(capabilities.primaryActionLabel).toBe("Unavailable");
    expect(capabilities.notice?.title).toBe("Claude session — unmanaged");
    expect(capabilities.managementDescription).toBe("Longhouse imported this Claude session.");
    expect(capabilities.composerDisabledReason).toBe(
      "This unmanaged Claude session is read-only in Longhouse.",
    );
    expect(capabilities.managedLaunchSuggestion?.command).toBe("longhouse claude");
  });

  it("points legacy Gemini sessions at Antigravity for new Google CLI work", () => {
    const capabilities = getSessionInteractionCapabilities({
      session: makeSession({
        provider: "gemini",
        capabilities: makeCapabilities(),
      }),
    });

    expect(capabilities.mode).toBe("unsupported");
    expect(capabilities.canChatFromBrowser).toBe(false);
    expect(capabilities.managementLabel).toBe("Unmanaged");
    expect(capabilities.capabilityLabel).toBe("Read only");
    expect(capabilities.composerDisabledReason).toBe(
      "This unmanaged Gemini session is read-only in Longhouse.",
    );
    expect(capabilities.managedLaunchSuggestion?.command).toBe("longhouse antigravity");
    expect(capabilities.managedLaunchSuggestion?.title).toBe(
      "Start the next Google CLI session with Antigravity",
    );
    expect(capabilities.primaryActionLabel).toBe("Unavailable");
  });
});
