import { createContext, useContext, type ReactNode } from "react";
import type { SessionStateFacts } from "@/shared/api/agents";
import { useWallClock } from "@/shared/hooks/useWallClock";
import { useWindowActivity } from "@/shared/hooks/useWindowActivity";
import { parseUTC } from "@/shared/lib/dateUtils";
import {
  getInteractionDisplayInfo,
  getToolInputRecord,
  getToolTargetText,
  getToolIntentLabel,
  isToolIntentAuthored,
  isAgentLaunchInteraction,
  getToolSummary,
  type ToolInteraction,
} from "@/shared/session/model";
import { sessionIsWorking, workClaimExpired } from "@/shared/session/sessionStatus";
import { formatElapsedClock } from "./sessionHeaderState";
import { RowHead, TargetAside } from "./RowHead";

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
  const windowActivity = useWindowActivity();
  const facts = liveness?.facts ?? null;
  const validUntilMs = Date.parse(facts?.activity.valid_until ?? "");
  const renderNowMs = Date.now();
  const nowMs = Math.max(
    renderNowMs,
    useWallClock(renderNowMs <= validUntilMs, windowActivity === "ambient" ? 15_000 : 1_000),
  );
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

  // Live, the composer already says the session is working: the row stays
  // quiet. It speaks only when it can no longer vouch for the call.
  const note =
    state === "live"
      ? null
      : state === "uncertain"
        ? sinceSignal
          ? `no signal from ${host} for ${sinceSignal}`
          : `no recent signal from ${host}`
        : "no output recorded";
  // Expanded, the detail pane has room for the full account.
  const detailNote =
    state === "live"
      ? `Waiting for return${sinceSignal ? ` · ${host} signal ${sinceSignal} ago` : ""}`
      : state === "uncertain"
        ? `Unconfirmed · ${sinceSignal ? `no signal from ${host} for ${sinceSignal}` : `no recent signal from ${host}`}`
        : `No output recorded${sinceSignal ? ` · ${host} signal ${sinceSignal} ago` : ""}`;

  return {
    enabled: liveness != null,
    state,
    elapsedSeconds,
    timeoutSeconds: declaredTimeoutSeconds(interaction, provider),
    note,
    detailNote,
  };
}

const LIVE_TAIL_LINES = 2;

/** The last non-blank lines of a running call's output so far, when the live preview supplied it. */
export function liveOutputTail(interaction: ToolInteraction): string | null {
  const text = interaction.callEvent?.tool_output_text || interaction.resultEvent?.tool_output_text;
  if (typeof text !== "string" || !text.trim()) return null;
  const lines = text
    .replace(/\r\n?/g, "\n")
    .split("\n")
    .map((line) => line.trimEnd())
    .filter((line) => line.trim().length > 0);
  return lines.length ? lines.slice(-LIVE_TAIL_LINES).join("\n") : null;
}

/** The budget shows once half of it is spent: a fast call never needs it. */
const BUDGET_VISIBLE_FRACTION = 0.5;

/**
 * A running call is the same one-line row it becomes when it returns: same
 * height, same label, same right column. Only the leading glyph (a dot that
 * pulses while the work claim is fresh) and the ticking clock say it is live,
 * so nothing reshapes when the result lands. The session's own health lives in
 * the composer; the row speaks only when the claim lapses.
 */
export function RunningCallRow({
  interaction,
  rowId,
  expanded,
  isSelected,
  onSelect,
  onToggleExpand,
  detail,
  preview = null,
  trail = false,
}: {
  interaction: ToolInteraction;
  rowId: string;
  expanded: boolean;
  isSelected: boolean;
  onSelect: () => void;
  onToggleExpand: () => void;
  detail: ReactNode;
  /** The output well a settled call shows outside a narrated step. */
  preview?: ReactNode;
  trail?: boolean;
}) {
  const run = useRunningCallState(interaction);
  const info = getInteractionDisplayInfo(interaction);
  const intent = getToolIntentLabel(interaction);
  const purpose = intent !== null && isToolIntentAuthored(interaction);
  const summary = getToolSummary(interaction);
  const command = getToolTargetText(interaction);
  const label = intent || summary || "running…";
  const fraction =
    run.timeoutSeconds != null && run.elapsedSeconds != null ? run.elapsedSeconds / run.timeoutSeconds : null;
  const showBudget = run.timeoutSeconds != null && fraction != null && fraction >= BUDGET_VISIBLE_FRACTION;
  const detailId = `${rowId}-detail`;
  const tail = expanded || preview ? null : liveOutputTail(interaction);
  const note = run.note;

  return (
    <div
      id={rowId}
      data-testid="session-timeline-row"
      data-row-kind="tool"
      data-tool-tier="action"
      data-status="pending"
      data-run-state={run.state}
      className={`tl-action tl-run${isSelected ? " is-selected" : ""}${expanded ? " is-expanded" : ""}${trail ? " tl-trail" : ""}${purpose ? " tl-action--purpose" : ""}`}
    >
      <RowHead
        className="tl-action__head"
        onActivate={() => {
          onSelect();
          onToggleExpand();
        }}
        aria-expanded={expanded}
        aria-controls={detailId}
        aria-label={purpose ? `${info.displayName}: ${label}` : undefined}
      >
        <span className="tl-action__icon tl-run__glyph" aria-hidden="true">
          <span className="tl-run__dot" />
        </span>
        {(trail && intent) || (purpose && !isAgentLaunchInteraction(interaction)) ? null : (
          <span className="tl-action__name" {...{ elementtiming: "longhouse-session-timeline-row" }}>
            {info.displayName}
          </span>
        )}
        <span className="tl-action__summary">{label}</span>
        {purpose ? <TargetAside target={command} text={label} /> : null}
        {note ? (
          <span className="tl-run__note" data-testid="running-call-note">
            {note}
          </span>
        ) : null}
        <span className="tl-action__meta">
          <span className="tl-action__time tl-run__clock" data-testid="running-call-clock">
            {run.elapsedSeconds != null ? formatElapsedClock(run.elapsedSeconds) : "…"}
            {showBudget ? (
              <span className={`tl-run__budget${fraction >= 0.9 ? " is-near" : ""}`}>
                {" "}/ {formatElapsedClock(run.timeoutSeconds as number)}
              </span>
            ) : null}
          </span>
          <span className={`tl-action__chev${expanded ? " is-open" : ""}`} aria-hidden="true">›</span>
        </span>
      </RowHead>
      {!expanded && preview ? preview : null}
      {tail ? (
        <pre className="tl-run__tail" data-testid="running-call-tail">
          {tail}
        </pre>
      ) : null}
      {expanded ? <div id={detailId}>{detail}</div> : null}
    </div>
  );
}
