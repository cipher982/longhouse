import { createContext, useContext, type ReactNode } from "react";
import type { SessionStateFacts } from "@/shared/api/agents";
import { useWallClock } from "@/shared/hooks/useWallClock";
import { parseUTC } from "@/shared/lib/dateUtils";
import {
  getInteractionDisplayInfo,
  getToolInputRecord,
  getToolIntentLabel,
  getToolSummary,
  type ToolInteraction,
} from "@/shared/session/model";
import { ACTIVITY_UNCERTAIN_LABEL, sessionIsWorking, workClaimExpired } from "@/shared/session/sessionStatus";
import { formatElapsedClock } from "./sessionHeaderState";

/**
 * The session evidence a running call reads. Motion follows the served work
 * claim (`sessionIsWorking`, gated on `valid_until`) and nothing else: when
 * the claim lapses the row goes still and says so. Absent (shared views,
 * tests), a running call keeps the plain row.
 */
export type TimelineLiveness = {
  facts: SessionStateFacts;
  /** Where the work runs ("cinder"), for "last signal from cinder". */
  host: string | null;
};

type LivenessContextValue = {
  liveness: TimelineLiveness | null;
  provider: string | null;
  /** Running calls the model has not moved past (no prose after them). */
  currentRunning: ReadonlySet<string>;
};

const TimelineLivenessContext = createContext<LivenessContextValue>({
  liveness: null,
  provider: null,
  currentRunning: new Set(),
});

export function TimelineLivenessProvider({
  value,
  children,
}: {
  value: LivenessContextValue;
  children: ReactNode;
}) {
  return <TimelineLivenessContext.Provider value={value}>{children}</TimelineLivenessContext.Provider>;
}

export function useTimelineLiveness(): LivenessContextValue {
  return useContext(TimelineLivenessContext);
}

/**
 * A declared timeout in seconds, only where the unit is known: `timeout_ms`
 * anywhere; Claude Code's Bash `timeout` (milliseconds in its tool schema);
 * OMP's bash `timeout` (seconds — a 240 budget around three 60 s sleeps in
 * server/tests_lite/fixtures/omp_background_result). Anything else draws no
 * track rather than a guessed one.
 */
export function declaredTimeoutSeconds(interaction: ToolInteraction, provider: string | null): number | null {
  const input = getToolInputRecord(interaction.callEvent?.tool_input_json);
  if (!input) return null;
  const positive = (value: unknown) => (typeof value === "number" && Number.isFinite(value) && value > 0 ? value : null);
  const ms = positive(input.timeout_ms);
  if (ms != null) return ms / 1_000;
  const timeout = positive(input.timeout);
  if (timeout == null) return null;
  if (provider === "claude") return timeout / 1_000;
  if (provider === "omp") return timeout;
  return null;
}

function agoWords(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s} s`;
  if (s < 3_600) return `${Math.round(s / 60)} min`;
  const hours = Math.floor(s / 3_600);
  const minutes = Math.round((s % 3_600) / 60);
  return minutes > 0 ? `${hours} h ${minutes} min` : `${hours} h`;
}

type RunningState = "live" | "uncertain" | "quiet";

/**
 * Where a running call stands right now, from served evidence only. Ticks once
 * a second (the composer clock's cadence and alignment) while the work claim
 * is still inside its window, then stops.
 */
export function useRunningCallState(interaction: ToolInteraction) {
  const { liveness, currentRunning, provider } = useTimelineLiveness();
  const facts = liveness?.facts ?? null;
  const validUntilMs = Date.parse(facts?.activity.valid_until ?? "");
  const renderNowMs = Date.now();
  const nowMs = Math.max(renderNowMs, useWallClock(renderNowMs <= validUntilMs, 1_000));
  const current = currentRunning.has(interaction.key);
  const working = facts ? sessionIsWorking(facts, nowMs) : false;
  const expired = facts ? workClaimExpired(facts, nowMs) : false;
  const state: RunningState = current && working ? "live" : current && expired ? "uncertain" : "quiet";

  const startMs = interaction.callEvent ? parseUTC(interaction.callEvent.timestamp).getTime() : Number.NaN;
  const lastSignalMs = Date.parse(facts?.activity.observed_at ?? "");
  const hasSignal = Number.isFinite(lastSignalMs);
  // Live, the clock runs to now. Otherwise it stops at the last signal: the
  // call is known to have run at least that long, and nothing more.
  const endMs = state === "live" ? nowMs : hasSignal && lastSignalMs > startMs ? lastSignalMs : null;
  const elapsedSeconds = Number.isFinite(startMs) && endMs != null ? Math.max(0, (endMs - startMs) / 1_000) : null;
  const sinceSignal = hasSignal ? agoWords((nowMs - lastSignalMs) / 1_000) : null;
  const host = liveness?.host ?? "the machine";

  const note =
    state === "live"
      ? `No output yet: the transcript records a call's output when the call returns.${sinceSignal ? ` Last signal from ${host} ${sinceSignal} ago.` : ""}`
      : state === "uncertain"
        ? `${ACTIVITY_UNCERTAIN_LABEL}: ${sinceSignal ? `no signal from ${host} for ${sinceSignal}` : `no recent signal from ${host}`}. The call may still be running; Longhouse can't confirm it.`
        : `No output recorded yet.${sinceSignal ? ` Last signal from ${host} ${sinceSignal} ago.` : ""}`;

  return {
    enabled: liveness != null,
    state,
    elapsedSeconds,
    timeoutSeconds: declaredTimeoutSeconds(interaction, provider),
    note,
  };
}

/**
 * A running call that looks alive: the agent's description as the headline,
 * the command beneath it in mono, an elapsed clock, and — when the call
 * declared a timeout — a thin track of how much of that budget is spent.
 */
export function RunningCallRow({
  interaction,
  rowId,
  expanded,
  isSelected,
  onSelect,
  onToggleExpand,
  detail,
}: {
  interaction: ToolInteraction;
  rowId: string;
  expanded: boolean;
  isSelected: boolean;
  onSelect: () => void;
  onToggleExpand: () => void;
  detail: ReactNode;
}) {
  const run = useRunningCallState(interaction);
  const info = getInteractionDisplayInfo(interaction);
  const intent = getToolIntentLabel(interaction);
  const summary = getToolSummary(interaction);
  const input = getToolInputRecord(interaction.callEvent?.tool_input_json);
  const command = typeof input?.command === "string" ? input.command : typeof input?.cmd === "string" ? input.cmd : null;
  const headline = intent || summary || info.displayName;
  const subline = command && command !== headline ? command : null;
  const fraction =
    run.timeoutSeconds != null && run.elapsedSeconds != null
      ? Math.min(1, run.elapsedSeconds / run.timeoutSeconds)
      : null;
  const detailId = `${rowId}-detail`;

  return (
    <div
      id={rowId}
      data-testid="session-timeline-row"
      data-row-kind="tool"
      data-tool-tier="action"
      data-status="pending"
      data-run-state={run.state}
      className={`tl-run${isSelected ? " is-selected" : ""}${expanded ? " is-expanded" : ""}`}
    >
      <button
        type="button"
        className="tl-run__head"
        onClick={() => {
          onSelect();
          onToggleExpand();
        }}
        aria-expanded={expanded}
        aria-controls={detailId}
      >
        <span className="tl-run__dot" aria-hidden="true" />
        <span className="tl-run__text">
          <span className="tl-run__headline" {...{ elementtiming: "longhouse-session-timeline-row" }}>
            {headline}
          </span>
          {subline ? (
            <span className="tl-run__command">
              <span className="tl-run__glyph" aria-hidden="true">{info.icon}</span> {subline}
            </span>
          ) : null}
        </span>
        <span className="tl-run__clock" data-testid="running-call-clock">
          <span className="tl-run__elapsed">
            {run.elapsedSeconds != null ? formatElapsedClock(run.elapsedSeconds) : "—"}
          </span>
          {run.timeoutSeconds != null ? (
            <span className="tl-run__budget">of {formatElapsedClock(run.timeoutSeconds)} timeout</span>
          ) : null}
        </span>
      </button>
      {fraction != null ? (
        <div className="tl-run__track" aria-hidden="true">
          <div className="tl-run__fill" style={{ width: `${(fraction * 100).toFixed(1)}%` }} />
          {run.state === "live" ? <div className="tl-run__shimmer" /> : null}
        </div>
      ) : null}
      {/* Expanded, the output block below says the same thing. */}
      {expanded ? <div id={detailId}>{detail}</div> : <div className="tl-run__note" data-testid="running-call-note">{run.note}</div>}
    </div>
  );
}
