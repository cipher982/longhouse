import type {
  AgentSession,
  AgentSessionStatus,
  SessionLivenessFacts,
  SessionRuntimeDisplay,
} from "../services/api/agents";

export type KnownPresenceState = "thinking" | "running" | "idle" | "needs_user" | "blocked" | "stalled";
export type RuntimeTruthTier = "none" | "stale" | "fresh" | "managed-local";
export type RuntimeTone = "inactive" | "active" | "thinking" | "running" | "blocked" | "stalled" | "idle" | "closed";
const TRANSCRIPT_SYNC_STATE = "syncing_transcript";

type TimelineRuntimeOverlay = {
  timeline_anchor_at?: string | null;
  runtime_source?: string | null;
  status?: AgentSessionStatus | string | null;
  presence_state?: string | null;
  presence_tool?: string | null;
  presence_updated_at?: string | null;
  last_live_at?: string | null;
  display_phase?: string | null;
  active_tool?: string | null;
  confidence?: string | null;
  runtime_display?: SessionRuntimeDisplay | null;
  runtime_facts?: SessionLivenessFacts | null;
  capabilities?: AgentSession["capabilities"] | null;
};

export type TimelineRuntimeSession = Pick<
  AgentSession,
  "ended_at" | "last_activity_at" | "timeline_anchor_at" | "capabilities"
> &
  Partial<TimelineRuntimeOverlay>;

/** Decide closed/open only from explicit lifecycle or terminal facts. */
export function isSessionClosed(
  session: Pick<AgentSession, "terminal_state"> & {
    runtime_display?: SessionRuntimeDisplay | null;
    runtime_facts?: SessionLivenessFacts | null;
  },
): boolean {
  const factsLifecycle = session.runtime_facts?.lifecycle?.state;
  if (factsLifecycle === "closed") {
    return true;
  }
  if (factsLifecycle === "open") {
    return false;
  }
  if (factsLifecycle != null) {
    return false;
  }
  const lifecycle = session.runtime_display?.lifecycle;
  if (lifecycle != null) {
    return lifecycle === "closed";
  }
  return (
    session.terminal_state === "session_ended" ||
    session.terminal_state === "process_gone" ||
    session.terminal_state === "user_closed"
  );
}

export interface SessionRuntimeState {
  status: string | null;
  presenceState: KnownPresenceState | null;
  presenceTool: string | null;
  lastLiveAt: string | null;
  runtimeSource: string | null;
  confidence: string | null;
  truthTier: RuntimeTruthTier;
  displayPhase: string;
  isLive: boolean;
  isExecuting: boolean;
  needsAttention: boolean;
  isIdle: boolean;
  isStalled: boolean;
  isManagedLocalTruth: boolean;
  hasSignal: boolean;
  tone: RuntimeTone;
  runtimeDisplay: SessionRuntimeDisplay | null;
  runtimeFacts: SessionLivenessFacts | null;
  factStatus: SessionFactStatus | null;
}

export type SessionControlPathLabel = "Managed" | "Unmanaged";

export interface SessionFactStatus {
  label: string;
  tone: RuntimeTone;
  seenAt: string | null;
  seenAtPrefix: "Closed" | "Checked" | "Last signal" | "Updated" | "Verified";
}

export function resolveSessionOwnershipLabel(
  runtime: SessionRuntimeState,
  fallback: SessionControlPathLabel = "Unmanaged",
): SessionControlPathLabel {
  const factControlPath = runtime.runtimeFacts?.control_path;
  if (factControlPath === "managed") {
    return "Managed";
  }
  if (factControlPath === "unmanaged") {
    return "Unmanaged";
  }
  const controlPath = runtime.runtimeDisplay?.control_path;
  if (controlPath === "managed") {
    return "Managed";
  }
  if (controlPath === "unmanaged") {
    return "Unmanaged";
  }
  return fallback;
}

function titleCaseWords(value: string): string {
  return value
    .split(/\s+/)
    .filter(Boolean)
    .map((word) => {
      if (word.length <= 3 && word === word.toUpperCase()) {
        return word;
      }
      return word.charAt(0).toUpperCase() + word.slice(1);
    })
    .join(" ");
}

function compactFactToolLabel(toolName: string | null | undefined): string | null {
  const raw = toolName?.trim();
  if (!raw) {
    return null;
  }
  const canonical = raw.split("__").pop() ?? raw;
  const normalized = canonical
    .replace(/^hatch_/, "")
    .replace(/^tool_/, "")
    .replace(/^mcp_/, "")
    .replace(/[-_.]+/g, " ")
    .trim();
  if (!normalized) {
    return null;
  }
  const lower = normalized.toLowerCase();
  if (lower === "codex") return "Codex";
  if (lower === "claude") return "Claude";
  if (lower === "antigravity") return "Antigravity";
  if (lower === "gemini") return "Gemini";
  if (lower === "default") return "Z.ai";
  if (lower === "shell" || lower === "bash" || lower === "terminal") return "Shell";
  if (
    lower === "edit" ||
    lower === "write" ||
    lower === "patch" ||
    lower === "apply patch" ||
    lower === "file change" ||
    lower === "filechange"
  ) {
    return "Edit";
  }
  return titleCaseWords(normalized);
}

function formatPhaseStatus(kind: string, tool: string | null | undefined): string {
  const phase = kind === "needs_user" ? "idle" : kind.replace(/[-_]+/g, " ");
  const compactTool = compactFactToolLabel(tool);
  if (compactTool && kind === "running") {
    return `Using ${compactTool}`;
  }
  if (compactTool && kind === "blocked") {
    return `${titleCaseWords(phase)} ${compactTool}`;
  }
  return titleCaseWords(phase);
}

function phaseTone(kind: string): RuntimeTone {
  if (kind === "thinking" || kind === "running" || kind === "blocked" || kind === "stalled") {
    return kind;
  }
  if (kind === "idle" || kind === "needs_user") {
    return "idle";
  }
  return "inactive";
}

function resolveSessionFactStatus(facts: SessionLivenessFacts | null): SessionFactStatus | null {
  if (!facts) {
    return null;
  }

  const lifecycle = facts.lifecycle?.state;
  const processState = facts.process_state;
  if (lifecycle === "closed" || processState === "closed") {
    return {
      label: "Closed",
      tone: "closed",
      seenAt: facts.lifecycle?.observed_at ?? facts.phase?.observed_at ?? facts.activity?.last_transcript_at ?? null,
      seenAtPrefix: "Closed",
    };
  }

  const phaseKind = facts.phase?.kind?.trim();
  if (phaseKind) {
    return {
      label: formatPhaseStatus(phaseKind, facts.phase?.tool),
      tone: phaseTone(phaseKind),
      seenAt: facts.phase?.observed_at ?? null,
      seenAtPrefix: "Updated",
    };
  }

  if (processState === "running" || facts.process?.status === "observed") {
    return {
      label: "Running",
      tone: "inactive",
      seenAt: facts.process?.observed_at ?? facts.process?.last_seen_at ?? null,
      seenAtPrefix: "Verified",
    };
  }

  return {
    label: "No live signal",
    tone: "inactive",
    seenAt: facts.activity?.last_runtime_signal_at ?? null,
    seenAtPrefix: facts.activity?.last_runtime_signal_at ? "Last signal" : "Checked",
  };
}

export function normalizePresenceState(state: string | null | undefined): KnownPresenceState | null {
  if (
    state === "thinking" ||
    state === "running" ||
    state === "idle" ||
    state === "needs_user" ||
    state === "blocked" ||
    state === "stalled"
  ) {
    return state;
  }
  return null;
}

function normalizeRuntimeSource(source: string | null | undefined): string | null {
  const trimmed = source?.trim();
  return trimmed ? trimmed : null;
}

function hasFreshRuntimeSignal({
  confidence,
  runtimeSource,
  presenceState,
}: {
  confidence: string | null;
  runtimeSource: string | null;
  presenceState: KnownPresenceState | null;
}): boolean {
  return (
    presenceState != null ||
    (confidence === "live" && runtimeSource !== "progress" && runtimeSource !== "fallback") ||
    runtimeSource === "semantic" ||
    runtimeSource === "managed_local_transport"
  );
}

function getRuntimeTruthTier(
  overlay: Partial<TimelineRuntimeOverlay> | null | undefined,
): RuntimeTruthTier {
  const confidence = overlay?.confidence ?? null;
  const presenceState = normalizePresenceState(overlay?.presence_state ?? null);
  const runtimeSource = normalizeRuntimeSource(overlay?.runtime_source ?? null);
  const hostReattachAvailable = overlay?.capabilities?.host_reattach_available === true;
  const hasFreshSignal = hasFreshRuntimeSignal({ confidence, runtimeSource, presenceState });

  if (hostReattachAvailable && hasFreshSignal && confidence !== "stale") {
    return "managed-local";
  }
  if (hasFreshSignal && confidence !== "stale") {
    return "fresh";
  }
  if (confidence === "stale" || runtimeSource === "fallback") {
    return "stale";
  }
  return "none";
}

function getDisplayPhase(
  presenceState: KnownPresenceState | null,
  presenceTool: string | null,
  status: string | null,
  explicitDisplayPhase?: string | null,
): string {
  if (explicitDisplayPhase?.trim()) {
    return explicitDisplayPhase.trim();
  }

  if (presenceState === "running") {
    return presenceTool ? `Running ${presenceTool}` : "Running";
  }
  if (presenceState === "thinking") {
    return "Thinking";
  }
  if (presenceState === "needs_user") {
    return "Idle";
  }
  if (presenceState === "blocked") {
    return presenceTool ? `Blocked on ${presenceTool}` : "Needs permission";
  }
  if (presenceState === "stalled") {
    return "Stalled";
  }
  if (presenceState === "idle") {
    return "Idle";
  }

  if (status === "idle") return "Idle";
  if (status === "completed") return "Completed";
  return "Inactive";
}

function getTone(
  presenceState: KnownPresenceState | null,
  {
    isIdle,
  }: {
    isIdle: boolean;
  },
): SessionRuntimeState["tone"] {
  if (presenceState === "stalled") {
    return "stalled";
  }
  if (presenceState === "blocked") {
    return "blocked";
  }
  if (presenceState === "needs_user") {
    return "idle";
  }
  if (presenceState === "running") {
    return "running";
  }
  if (presenceState === "thinking") {
    return "thinking";
  }
  if (isIdle) {
    return "idle";
  }
  return "inactive";
}

export function resolveSessionRuntimeState(
  session: TimelineRuntimeSession,
): SessionRuntimeState {
  const serverDisplay = session.runtime_display ?? null;
  const runtimeFacts = session.runtime_facts ?? null;
  const rawFactStatus = resolveSessionFactStatus(runtimeFacts);
  const displayOverridesFacts =
    serverDisplay?.state === TRANSCRIPT_SYNC_STATE && runtimeFacts?.lifecycle?.state !== "closed";
  const factStatus = displayOverridesFacts ? null : rawFactStatus;
  const hasFacts = runtimeFacts != null;
  const hasFactsForRuntime = hasFacts && !displayOverridesFacts;
  const sessionTruthTier = hasFactsForRuntime ? "none" : getRuntimeTruthTier(session);
  const status = session.status ?? null;
  const isClosed = hasFacts ? runtimeFacts?.lifecycle?.state === "closed" : serverDisplay?.lifecycle === "closed";
  const rawPresenceState = hasFactsForRuntime ? null : normalizePresenceState(serverDisplay ? serverDisplay.state : session.presence_state ?? null);
  const presenceState = isClosed ? null : rawPresenceState;
  const presenceTool = hasFactsForRuntime ? null : (session.active_tool ?? session.presence_tool ?? null);
  const lastLiveAt =
    hasFactsForRuntime
      ? null
      : (session.last_live_at ??
        session.presence_updated_at ??
        (presenceState ? session.last_activity_at ?? null : null));
  const runtimeSource = hasFactsForRuntime ? null : normalizeRuntimeSource(session.runtime_source ?? null);
  const confidence = hasFactsForRuntime ? null : (session.confidence ?? null);
  const truthTier = hasFactsForRuntime ? "none" : (normalizeRuntimeTruthTier(serverDisplay?.truth_tier) ?? sessionTruthTier);

  const isExecuting = hasFactsForRuntime || isClosed
    ? false
    : (serverDisplay?.is_executing ?? (presenceState === "thinking" || presenceState === "running"));
  const needsAttention = hasFactsForRuntime || isClosed
    ? false
    : (serverDisplay?.needs_attention ?? (presenceState === "blocked" || presenceState === "stalled"));

  const isLive = hasFactsForRuntime || isClosed ? false : (serverDisplay?.is_live ?? isExecuting);
  const isIdle = isClosed
    ? true
    : hasFactsForRuntime
    ? false
    : (serverDisplay?.is_idle ?? (presenceState === "idle" || presenceState === "needs_user"));
  const isStalled = hasFactsForRuntime || isClosed ? false : (serverDisplay?.is_stalled ?? presenceState === "stalled");
  const hasSignal = hasFactsForRuntime
    ? factStatus != null
    : (serverDisplay?.has_signal ?? (truthTier !== "none" || presenceState != null || status != null || lastLiveAt != null));

  const displayPhase =
    factStatus?.label ??
    (isClosed
      ? "Closed"
      : (serverDisplay?.phase_label ??
          getDisplayPhase(
            presenceState,
            presenceTool,
            status,
            session.display_phase ?? null,
          )));
  const tone = factStatus?.tone ?? (isClosed ? "closed" : (normalizeRuntimeTone(serverDisplay?.tone) ?? getTone(presenceState, { isIdle })));

  return {
    status,
    presenceState,
    presenceTool,
    lastLiveAt,
    confidence,
    runtimeSource,
    truthTier,
    displayPhase,
    isLive,
    isExecuting,
    needsAttention,
    isIdle,
    isStalled,
    isManagedLocalTruth: hasFactsForRuntime ? false : (serverDisplay?.is_managed_local_truth ?? truthTier === "managed-local"),
    hasSignal,
    tone,
    runtimeDisplay: serverDisplay,
    runtimeFacts,
    factStatus,
  };
}

function normalizeRuntimeTruthTier(value: string | null | undefined): RuntimeTruthTier | null {
  if (
    value === "none" ||
    value === "stale" ||
    value === "fresh" ||
    value === "managed-local"
  ) {
    return value;
  }
  return null;
}

function normalizeRuntimeTone(value: string | null | undefined): RuntimeTone | null {
  if (
    value === "inactive" ||
    value === "active" ||
    value === "thinking" ||
    value === "running" ||
    value === "blocked" ||
    value === "stalled" ||
    value === "idle" ||
    value === "closed"
  ) {
    return value;
  }
  return null;
}
