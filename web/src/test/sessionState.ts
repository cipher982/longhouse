import type { SessionStateFacts } from "../services/api/agents";

type SessionStateOptions = {
  closed?: boolean;
  activity?: SessionStateFacts["activity"]["state"];
  access?: "live_control" | "reattach" | "observe_only" | "search_only" | null;
  pendingInteraction?: boolean;
  observedAt?: string | null;
  tool?: string | null;
  sendAvailable?: boolean;
};

export function makeSessionStateFacts(options: SessionStateOptions = {}): SessionStateFacts {
  const activity = options.activity ?? "unknown";
  const access = options.access === undefined ? "search_only" : options.access;
  const available = { state: "available" as const };
  const unavailable = { state: "unavailable" as const, reason: "not_granted" };
  const sendAvailable = options.sendAvailable ?? access === "live_control";
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
          : activity === "blocked" || activity === "stalled"
            ? { key: activity, label: activity === "blocked" ? "Blocked" : "Stalled", tone: activity }
            : { key: "activity_unknown", label: "Activity unknown", tone: "quiet" };
  const accessLabels = {
    live_control: { label: "Live control", tone: "connected" },
    reattach: { label: "Reattach", tone: "reattach" },
    observe_only: { label: "Observe only", tone: "observe" },
    search_only: { label: "Search only", tone: "search" },
  } as const;

  return {
    state_contract_version: 1,
    presentation_policy_version: 1,
    mode: access === "live_control" || access === "reattach" ? "helm" : "shadow",
    disposition: {
      state: options.closed ? "closed" : "open",
      closed_at: options.closed ? (options.observedAt ?? "2026-03-21T12:00:00Z") : null,
    },
    run: options.closed ? { lifecycle: "ended" } : { lifecycle: "running" },
    activity: {
      state: activity,
      observed_at: options.observedAt,
      tool: options.tool ?? (activity === "executing" ? "Shell" : null),
    },
    control: {
      ownership: access === "live_control" || access === "reattach" ? "owned" : "unowned",
      connection: access === "live_control" ? "connected" : access === "reattach" ? "disconnected" : "not_applicable",
      actions: {
        send_input: sendAvailable ? available : unavailable,
        interrupt: access === "live_control" ? available : unavailable,
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
    host: { state: "unknown" },
    presentation: {
      primary,
      access: access ? { key: access, ...accessLabels[access] } : null,
      transcript: null,
    },
  };
}
