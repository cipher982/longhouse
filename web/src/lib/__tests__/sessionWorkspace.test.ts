import { readFileSync } from "node:fs";
import { resolve } from "node:path";
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
import { makeSessionStateFacts } from "../../test/sessionState";

type TranscriptPreviewFixture = {
  cases: Array<{
    name: string;
    session: Pick<AgentSession, "id" | "transcript_preview">;
    projection: {
      items: AgentSessionProjectionItem[];
    };
    expectations: {
      rendered_event_ids: number[];
      rendered_message_texts: string[];
      renders_preview: boolean;
    };
  }>;
};

function makeCapabilities(overrides: Partial<SessionCapabilities> = {}): SessionCapabilities {
  return {
    live_control_available: false,
    host_reattach_available: false,
    reply_to_live_session_available: false,
    ...overrides,
  };
}

function makeSession(overrides: Partial<AgentSession> = {}): AgentSession {
  const capabilities = overrides.capabilities ?? makeCapabilities();
  const runtimeDisplay = overrides.runtime_display;
  const access = capabilities.live_control_available
    ? "live_control"
    : capabilities.host_reattach_available
      ? "reattach"
      : "search_only";
  return {
    id: "session-1",
    provider: "claude",
    project: "zerg",
    device_id: "cinder",
    environment: "development",
    cwd: "/Users/example/git/zerg",
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
    capabilities,
    session_state: makeSessionStateFacts({
      closed: runtimeDisplay?.lifecycle === "closed",
      access,
      activity:
        runtimeDisplay?.state === "running"
          ? "executing"
          : runtimeDisplay?.state === "thinking"
            ? "thinking"
            : runtimeDisplay?.state === "idle" || runtimeDisplay?.state === "needs_user"
              ? "quiescent"
              : runtimeDisplay?.state === "blocked" || runtimeDisplay?.state === "stalled"
                ? runtimeDisplay.state
                : "unknown",
      pendingInteraction: runtimeDisplay?.needs_attention,
      tool: runtimeDisplay?.compact_tool_label,
      sendAvailable: capabilities.reply_to_live_session_available === true,
    }),
    loop_mode: "assist",
    ...overrides,
  };
}

function loadTranscriptPreviewFixture(): TranscriptPreviewFixture {
  const fixturePath = resolve(process.cwd(), "../tests/fixtures/session-transcript-preview/rendering.json");
  return JSON.parse(readFileSync(fixturePath, "utf8")) as TranscriptPreviewFixture;
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

  it("preserves live Console tool metadata in the synthetic event", () => {
    const session = makeSession({
      transcript_preview: {
        event_id: 7,
        text: "/tmp/project",
        role: "assistant",
        tool_name: "exec",
        tool_input_json: { command: "pwd" },
        tool_output_text: "/tmp/project\n",
        tool_call_id: "exec-1",
        tool_call_state: "completed",
        event_origin: "live_provisional",
        timestamp: "2026-07-16T18:00:00Z",
        is_provisional: true,
        is_complete: true,
        content_cursor: "codex_console_live:exec-1:2",
        is_stale: false,
        stale_reason: null,
      },
    });

    const projected = projectionItemsWithTranscriptPreview([], session);

    expect(projected).toHaveLength(2);
    expect(projected[0]?.event).toMatchObject({
      role: "assistant",
      tool_name: "exec",
      tool_input_json: { command: "pwd" },
      tool_call_id: "exec-1",
      tool_call_state: "completed",
    });
    expect(projected[1]?.event).toMatchObject({
      role: "tool",
      tool_output_text: "/tmp/project\n",
      tool_call_id: "exec-1",
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

describe("shared transcript preview fixtures", () => {
  it.each(loadTranscriptPreviewFixture().cases)("$name", (fixtureCase) => {
    const session = makeSession(fixtureCase.session);
    const items = projectionItemsWithTranscriptPreview(fixtureCase.projection.items, session);
    const model = buildTimelineModel(items);
    const messages = model.items.filter((item) => item.kind === "message");

    expect(messages.map((item) => item.event.id)).toEqual(fixtureCase.expectations.rendered_event_ids);
    expect(messages.map((item) => item.event.content_text)).toEqual(fixtureCase.expectations.rendered_message_texts);
    expect(messages.some((item) => item.event.id < 0)).toBe(fixtureCase.expectations.renders_preview);
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
  function makeCall(state: "running" | "completed" | "dropped" | null = null): AgentEvent {
    return {
      id: 1,
      role: "assistant",
      content_text: null,
      tool_name: "Bash",
      tool_input_json: null,
      tool_output_text: null,
      tool_call_id: "tc-1",
      tool_call_state: state,
      timestamp: "2026-03-22T22:00:00Z",
      in_active_context: true,
    };
  }

  function makeInteraction(state: "running" | "completed" | "dropped" | null = null): ToolInteraction {
    const call = makeCall(state);
    return {
      key: "id:tc-1",
      toolName: "Bash",
      callEvent: call,
      resultEvent: null,
      pairing: "id",
      anchorId: 1,
      timestamp: call.timestamp,
    };
  }

  it("returns true only when the server-emitted state is 'dropped'", () => {
    expect(isToolInteractionDropped(makeInteraction("dropped"))).toBe(true);
  });

  it("returns false when state is running", () => {
    expect(isToolInteractionDropped(makeInteraction("running"))).toBe(false);
  });

  it("returns false when state is completed", () => {
    expect(isToolInteractionDropped(makeInteraction("completed"))).toBe(false);
  });

  it("returns false when state is missing", () => {
    expect(isToolInteractionDropped(makeInteraction(null))).toBe(false);
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
    expect(capabilities.capabilityLabel).toBe("Live control");
    expect(capabilities.composerDisabledReason).toBeNull();
    expect(capabilities.sendDisabledReason).toBeNull();
    expect(capabilities.primaryActionLabel).toBe("Open live dock");
    expect(capabilities.submitLabel).toBe("Send");
  });

  it("uses Console start_turn independently from Helm send_input", () => {
    const consoleCapabilities = makeCapabilities({
      composer_enabled: false,
      can_start_turn: false,
      can_send_input: false,
    });
    const enabled = getSessionInteractionCapabilities({
      session: makeSession({
        capabilities: consoleCapabilities,
        session_state: makeSessionStateFacts({
          mode: "console",
          access: "live_control",
          startTurnAvailable: true,
          sendAvailable: false,
        }),
      }),
    });
    expect(enabled.mode).toBe("managed_local");
    expect(enabled.canChatFromBrowser).toBe(true);

    const legacyOnly = getSessionInteractionCapabilities({
      session: makeSession({
        capabilities: makeCapabilities({ composer_enabled: true, can_start_turn: true }),
        session_state: makeSessionStateFacts({
          mode: "console",
          access: "live_control",
          startTurnAvailable: false,
          sendAvailable: false,
        }),
      }),
    });
    expect(legacyOnly.mode).toBe("managed_local_unavailable");
    expect(legacyOnly.canChatFromBrowser).toBe(false);
  });

  it("uses canonical control ownership instead of runtime display aliases", () => {
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
        session_state: makeSessionStateFacts({ access: null, activity: "quiescent" }),
      }),
    });

    expect(capabilities.mode).toBe("unsupported");
    expect(capabilities.managementLabel).toBe("Unmanaged");
    expect(capabilities.managedLaunchSuggestion).not.toBeNull();
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
        session_state: makeSessionStateFacts({
          access: "live_control",
          activity: "quiescent",
          sendAvailable: false,
        }),
      }),
    });

    expect(capabilities.mode).toBe("unsupported");
    expect(capabilities.canChatFromBrowser).toBe(false);
    expect(capabilities.managementLabel).toBe("Managed");
    expect(capabilities.managedLaunchSuggestion).toBeNull();
    expect(capabilities.capabilityLabel).toBe("Live control");
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
    expect(capabilities.sendDisabledReason).toBe("not_granted");
    expect(capabilities.composerDisabledReason).toMatch(/managed Codex session is read-only/i);
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
    expect(capabilities.capabilityLabel).toBe("Reattach");
    // Reattach-available is not an outage: say what it is and what fixes it,
    // rather than asserting a reconnect that is not what is missing.
    expect(capabilities.composerDisabledReason).toMatch(/isn't attached/i);
    expect(capabilities.composerDisabledReason).toMatch(/Reattach/i);
    expect(capabilities.composerDisabledReason).not.toMatch(/engine reconnects/i);
    expect(capabilities.primaryActionLabel).toBe("Unavailable");
    expect(capabilities.notice?.title).toBe("Reattach");
  });

  it("treats an ended Helm run as a resting state, not a control fault", () => {
    // Ending the run clears the durable run id, which by design rejects every
    // run-bound control head, so control reads owned/unknown. That used to
    // reach "Longhouse can't confirm the control link" with a warning tone —
    // a lease diagnostic shown as a fault for exiting a terminal.
    const state = makeSessionStateFacts({ access: "reattach" });
    const endedState = {
      ...state,
      mode: "helm" as const,
      run: { lifecycle: "ended" as const },
      presentation: { ...state.presentation, access: null },
      control: {
        ...state.control,
        connection: "unknown" as const,
        actions: {
          ...state.control.actions,
          reattach: { state: "unavailable" as const, reason: "not_granted" },
          resume: { state: "available" as const },
        },
      },
    };
    const capabilities = getSessionInteractionCapabilities({
      session: makeSession({
        provider: "codex",
        session_state: endedState,
        capabilities: makeCapabilities({ host_reattach_available: true }),
      }),
    });

    expect(capabilities.capabilityLabel).toBe("Ended");
    expect(capabilities.capabilityVariant).toBe("neutral");
    expect(capabilities.notice?.title).toBe("Run ended");
    expect(capabilities.composerDisabledReason).toMatch(/run has ended/i);
    expect(capabilities.composerDisabledReason).toMatch(/Resume/i);
    expect(capabilities.composerDisabledReason).not.toMatch(/confirm the control link/i);
  });

  it("treats a launch still in flight as starting, not as a control fault", () => {
    // The banner said "Starting session on cinder" while the chip under it said
    // "Longhouse can't confirm the control link", because the reducer read no
    // launch fact at all. Nothing has attached yet, so there is no control path
    // for anything to be wrong with.
    const capabilities = getSessionInteractionCapabilities({
      session: makeSession({
        provider: "codex",
        session_state: makeSessionStateFacts({ access: null, mode: "helm", launchState: "dispatched" }),
        capabilities: makeCapabilities(),
      }),
    });

    expect(capabilities.capabilityLabel).toBe("Launching");
    expect(capabilities.capabilityVariant).toBe("neutral");
    expect(capabilities.notice?.title).toBe("Starting");
    expect(capabilities.composerDisabledReason).toMatch(/starting this Codex session/i);
    expect(capabilities.composerDisabledReason).not.toMatch(/confirm the control link/i);
  });

  it("calls a session Longhouse is starting managed before a control path claims it", () => {
    // A `launch` fact exists only for a launch Longhouse itself initiated, so
    // it proves ownership in the window before the control axis reports it.
    // Ranked below ownership, a starting session read as somebody else's.
    const capabilities = getSessionInteractionCapabilities({
      session: makeSession({
        provider: "codex",
        session_state: makeSessionStateFacts({ access: null, mode: "helm", launchState: "pending" }),
        capabilities: makeCapabilities(),
      }),
    });

    expect(capabilities.managementLabel).toBe("Managed");
    expect(capabilities.managementDescription).toMatch(/starting it now/i);
    expect(capabilities.description).not.toMatch(/unmanaged/i);
  });

  it("names the launch error instead of inventing a lease diagnostic", () => {
    const capabilities = getSessionInteractionCapabilities({
      session: makeSession({
        provider: "codex",
        session_state: makeSessionStateFacts({
          access: null,
          mode: "helm",
          launchState: "failed",
          launchErrorCode: "launch_timeout",
          launchErrorMessage: "Machine Agent did not report back before lease expired",
        }),
        capabilities: makeCapabilities(),
      }),
    });

    expect(capabilities.capabilityLabel).toBe("Launch failed");
    expect(capabilities.notice?.title).toBe("Launch failed");
    expect(capabilities.composerDisabledReason).toMatch(/did not report back/i);
    expect(capabilities.composerDisabledReason).not.toMatch(/confirm the control link/i);
    // A launch that actually failed is a fault; only the in-flight state is not.
    expect(capabilities.capabilityVariant).toBe("warning");
  });

  it("lets Closed outrank a launch that never landed", () => {
    const capabilities = getSessionInteractionCapabilities({
      session: makeSession({
        provider: "codex",
        session_state: makeSessionStateFacts({
          access: null,
          mode: "helm",
          closed: true,
          launchState: "failed",
        }),
        capabilities: makeCapabilities(),
      }),
    });

    expect(capabilities.capabilityLabel).toBe("Closed");
    expect(capabilities.composerDisabledReason).toMatch(/session is closed/i);
  });

  it("lets Closed and an unreachable machine outrank an ended run", () => {
    const base = makeSessionStateFacts({ access: "reattach" });
    const ended = {
      ...base,
      mode: "helm" as const,
      run: { lifecycle: "ended" as const },
      presentation: { ...base.presentation, access: null },
      control: {
        ...base.control,
        connection: "unknown" as const,
        actions: {
          ...base.control.actions,
          reattach: { state: "unavailable" as const, reason: "not_granted" },
          resume: { state: "unavailable" as const, reason: "machine_offline" },
        },
      },
    };

    const closed = getSessionInteractionCapabilities({
      session: makeSession({
        provider: "codex",
        session_state: { ...ended, disposition: { state: "closed", closed_at: "2026-03-21T12:00:00Z" } },
        capabilities: makeCapabilities({ host_reattach_available: true }),
      }),
    });
    expect(closed.capabilityLabel).toBe("Closed");
    expect(closed.notice?.title).not.toBe("Run ended");

    const offline = getSessionInteractionCapabilities({
      session: makeSession({
        provider: "codex",
        session_state: { ...ended, host: { state: "offline" } },
        capabilities: makeCapabilities({ host_reattach_available: true }),
      }),
    });
    // The machine being unreachable is the fact the user can act on, and it is
    // why Resume is unavailable. "Run ended" would bury it.
    expect(offline.composerDisabledReason).toMatch(/machine running this Codex session is offline/i);
  });

  it("says a closed session is closed instead of naming a lease", () => {
    const base = makeSessionStateFacts({ access: "reattach", closed: true });
    const capabilities = getSessionInteractionCapabilities({
      session: makeSession({
        provider: "codex",
        session_state: {
          ...base,
          mode: "helm",
          control: { ...base.control, connection: "unknown" },
        },
        capabilities: makeCapabilities({ host_reattach_available: true }),
      }),
    });

    expect(capabilities.capabilityLabel).toBe("Closed");
    expect(capabilities.composerDisabledReason).toMatch(/session is closed/i);
    expect(capabilities.composerDisabledReason).not.toMatch(/confirm the control link/i);
    expect(capabilities.composerDisabledReason).not.toMatch(/run has ended/i);
  });

  it("names the offline machine rather than offering a reattach it cannot reach", () => {
    // Reattach eligibility is projected from a durable connection row that
    // never consults host state. Server, web and iOS all have to answer this
    // combination the same way, or the dock and the composer describe one
    // session differently.
    const base = makeSessionStateFacts({ access: "reattach" });
    const capabilities = getSessionInteractionCapabilities({
      session: makeSession({
        provider: "codex",
        session_state: { ...base, mode: "helm", host: { state: "offline" } },
        capabilities: makeCapabilities({ host_reattach_available: true }),
      }),
    });

    expect(capabilities.composerDisabledReason).toMatch(/machine running this Codex session is offline/i);
    expect(capabilities.composerDisabledReason).not.toMatch(/isn't attached/i);
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

    expect(capabilities.mode).toBe("managed_local");
    expect(capabilities.placeholder).toBe("Server placeholder");
    expect(capabilities.composerDisabledReason).toBeNull();
    expect(capabilities.sendDisabledReason).toBeNull();
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
    expect(capabilities.capabilityLabel).toBe("Reattach");
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
    expect(capabilities.capabilityLabel).toBe("Search only");
    expect(capabilities.primaryActionLabel).toBe("Unavailable");
    expect(capabilities.notice?.title).toBe("Claude session — unmanaged");
    expect(capabilities.managementDescription).toBe("Longhouse imported this Claude session.");
    expect(capabilities.composerDisabledReason).toBe(
      "This unmanaged Claude session is read-only in Longhouse.",
    );
    expect(capabilities.managedLaunchSuggestion?.command).toBe("longhouse claude");
  });

  it("maps a legacy gemini Shadow session to Antigravity labels without granting control", () => {
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
    expect(capabilities.composerDisabledReason).toBe(
      "This unmanaged Antigravity session is read-only in Longhouse.",
    );
    expect(capabilities.managedLaunchSuggestion?.command).toBe("longhouse antigravity");
    expect(capabilities.primaryActionLabel).toBe("Unavailable");
  });
});
