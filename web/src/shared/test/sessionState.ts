import type { SessionStateFacts } from "@/shared/api/agents";

type SessionStateOptions = {
  closed?: boolean;
  activity?: SessionStateFacts["activity"]["state"];
  access?: "live_control" | "reattach" | "observe_only" | "search_only" | null;
  pendingInteraction?: boolean;
  observedAt?: string | null;
  tool?: string | null;
  sendAvailable?: boolean;
  startTurnAvailable?: boolean;
  interruptAvailable?: boolean;
  mode?: SessionStateFacts["mode"];
  terminalAttached?: boolean | null;
  unread?: boolean;
  lastResultAt?: string | null;
  lastResultOutcome?: string | null;
  activityValidUntil?: string | null;
  hostState?: NonNullable<SessionStateFacts["host"]>["state"];
  launchState?: NonNullable<SessionStateFacts["launch"]>["state"] | null;
  launchErrorCode?: string | null;
  launchErrorMessage?: string | null;
};

type ServedSignal = NonNullable<SessionStateFacts["presentation"]["signal"]>;
type SignalPrimary = { key: string; tone: string } | null | undefined;

/**
 * Mirror of the server's `session_state_contract._signal`, for fixtures that
 * hand-build a presentation. Fixtures must carry the field the server serves;
 * this keeps them from drifting from the server's mapping.
 */
export function mirrorServedSignal(
  primary: SignalPrimary,
  evidence: {
    activity?: { state?: string | null; valid_until?: string | null } | null;
    delegation?: { valid_until?: string | null } | null;
  } = {},
): ServedSignal {
  if (!primary) return { state: "unknown", valid_until: null };
  if (primary.key === "closed") return { state: "closed", valid_until: null };
  if (primary.tone === "blocked" || primary.tone === "stalled") {
    return { state: "attention", valid_until: primary.key === "stalled" ? (evidence.activity?.valid_until ?? null) : null };
  }
  if (primary.tone === "running" || primary.tone === "thinking" || primary.tone === "active") {
    const activityState = evidence.activity?.state;
    const validUntil = primary.key === "delegated_work"
      ? (evidence.delegation?.valid_until ?? null)
      : activityState === "thinking" || activityState === "executing"
        ? (evidence.activity?.valid_until ?? null)
        : null;
    return { state: "working", valid_until: validUntil };
  }
  if (primary.key === "idle" || primary.key === "ready" || primary.key === "ended") {
    return { state: "quiet", valid_until: null };
  }
  return { state: "unknown", valid_until: null };
}

export function makeSessionStateFacts(options: SessionStateOptions = {}): SessionStateFacts {
  const activity = options.activity ?? "unknown";
  const access = options.access === undefined ? "search_only" : options.access;
  const available = { state: "available" as const };
  const unavailable = { state: "unavailable" as const, reason: "not_granted" };
  const sendAvailable = options.sendAvailable ?? access === "live_control";
  const mode = options.mode ?? (access === "live_control" || access === "reattach" ? "helm" : "shadow");
  const primary = options.closed
    ? { key: "closed", label: "Closed", tone: "closed", observed_at: options.observedAt }
    : options.pendingInteraction
      ? { key: "needs_answer", label: "Needs answer", tone: "blocked", observed_at: options.observedAt }
    : activity === "thinking"
      ? { key: "thinking", label: "Thinking", tone: "thinking", observed_at: options.observedAt }
      : activity === "executing"
        ? { key: "executing", label: "Using Shell", tone: "running", observed_at: options.observedAt }
        : activity === "quiescent"
          ? { key: "idle", label: "Idle", tone: "idle", observed_at: options.observedAt }
          : activity === "stalled"
            ? { key: "stalled", label: "Stalled", tone: "stalled" }
            // The server has no `blocked` rung: a raw provider block is not a headline.
            : { key: "activity_unknown", label: "Activity unknown", tone: "quiet" };

  const signal = mirrorServedSignal(primary, {
    activity: { state: activity, valid_until: options.activityValidUntil ?? null },
  });
  const accessLabels = {
    live_control: { label: "Live control", tone: "connected" },
    reattach: { label: "Reattach", tone: "reattach" },
    observe_only: { label: "Observe only", tone: "observe" },
    search_only: { label: "Search only", tone: "search" },
  } as const;

  // Mirror the server's _working_set rule so fixtures cannot drift from it.
  const terminalAttached = options.terminalAttached ?? null;
  const workingSet: SessionStateFacts["working_set"] = options.closed
    ? "history"
    : options.pendingInteraction
      || activity === "thinking"
      || activity === "executing"
      || terminalAttached === true
      ? "open"
      : "history";

  // Mirror the server's `_project_run`: a launch that has not landed yet has no
  // run row, and the projector reports its run as `starting` rather than
  // inventing one. A fixture with a launch pending and a run already running is
  // a state the server cannot emit.
  const launchInFlight = options.launchState === "pending" || options.launchState === "dispatched";

  return {
    state_contract_version: 1,
    presentation_policy_version: 1,
    mode,
    disposition: {
      state: options.closed ? "closed" : "open",
      closed_at: options.closed ? (options.observedAt ?? "2026-03-21T12:00:00Z") : null,
    },
    launch: options.launchState
      ? {
          state: options.launchState,
          error_code: options.launchErrorCode ?? null,
          error_message: options.launchErrorMessage ?? null,
        }
      : null,
    run: launchInFlight
      ? { lifecycle: "starting" }
      : options.closed
        ? { lifecycle: "ended" }
        : { lifecycle: "running" },
    activity: {
      state: activity,
      observed_at: options.observedAt,
      tool: options.tool ?? (activity === "executing" ? "Shell" : null),
      valid_until: options.activityValidUntil ?? null,
    },
    working_set: workingSet,
    unread: options.unread ?? false,
    last_result_at: options.lastResultAt ?? null,
    last_result_outcome: options.lastResultOutcome ?? null,
    control: {
      ownership: access === "live_control" || access === "reattach" ? "owned" : "unowned",
      terminal_attached: terminalAttached,
      connection: mode === "console"
        ? "not_applicable"
        : access === "live_control"
          ? "connected"
          : access === "reattach"
            ? "disconnected"
            : "not_applicable",
      actions: {
        start_turn: options.startTurnAvailable ? available : unavailable,
        send_input: sendAvailable ? available : unavailable,
        interrupt: (options.interruptAvailable ?? access === "live_control") ? available : unavailable,
        terminate: access === "live_control" ? available : unavailable,
        reattach: access === "reattach" ? available : unavailable,
        resume: unavailable,
      },
    },
    pending_interaction: options.pendingInteraction
      ? { id: "interaction-1", kind: "question", can_respond: true }
      : null,
    transcript: {
      convergence: "current",
      searchable: true,
      live_observation: access === "observe_only",
    },
    host: { state: options.hostState ?? "unknown" },
    presentation: {
      primary,
      signal,
      access: access ? { key: access, ...accessLabels[access] } : null,
      transcript: null,
    },
  };
}
