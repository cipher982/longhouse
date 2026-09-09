import { useEffect, useRef, useState } from "react";
import type { AgentSession } from "../../services/api/agents";
import type { SessionInteractionCapabilities } from "../../lib/sessionWorkspace";
import type { SessionActivityFeed } from "../../lib/sessionActivityFeed";
import { useWallClock } from "../../hooks/useWallClock";
import { activityEvidenceIsLive } from "../../lib/activityEvidence";
import { resolveSessionRuntimeState } from "../../lib/sessionRuntime";
import {
  getRuntimeDisplayCopy,
  getRuntimeMetaLabel,
  getRuntimeOutcomeLabel,
} from "../../lib/sessionUtils";
import { SessionLedger, type SessionLedgerState } from "./SessionLedger";
import "./SessionLedger.css";

interface SessionRuntimeStripProps {
  session: AgentSession;
  interaction: Pick<
    SessionInteractionCapabilities,
    "mode" | "isManagedLocalSession" | "capabilityLabel"
  >;
  startedLabel?: string | null;
  variant?: "inline" | "block" | "dock" | "bar";
  testId?: string;
  /** Per-frame stream feed; absent when the caller has no live stream. */
  activityFeed?: SessionActivityFeed | null;
  /** Viewer subscription state from the existing workspace stream. */
  streamConnected?: boolean;
}

/** Build Ledger copy strictly from canonical facts plus the viewer's stream evidence. */
export function buildSessionLedgerState(
  session: AgentSession,
  interaction: SessionRuntimeStripProps["interaction"],
  nowMs: number,
  streamConnected: boolean,
  activityFeed: SessionActivityFeed | null = null,
): SessionLedgerState {
  const runtime = resolveSessionRuntimeState(session);
  const facts = runtime.stateFacts;
  const display = getRuntimeDisplayCopy(runtime);
  const evidenceLive = activityEvidenceIsLive(facts.activity, nowMs);
  const pending = facts.pending_interaction != null;
  const rawProviderWorking =
    facts.activity.state === "thinking" || facts.activity.state === "executing";
  const openSession = facts.working_set === "open";
  const hostConcern =
    openSession &&
    (facts.host.state === "offline" || facts.host.state === "stale");
  const transcriptConcern =
    openSession && facts.transcript.convergence === "lagging";
  const providerWorking = evidenceLive && rawProviderWorking;
  const viewerNeedsDisclosure =
    (!streamConnected &&
      (rawProviderWorking ||
        pending ||
        facts.activity.state === "blocked" ||
        facts.activity.state === "stalled")) ||
    hostConcern ||
    transcriptConcern ||
    (rawProviderWorking && !evidenceLive);
  const tone: SessionLedgerState["tone"] = pending
    ? "attention"
    : viewerNeedsDisclosure
      ? "unknown"
      : evidenceLive &&
          (runtime.tone === "blocked" || runtime.tone === "stalled")
        ? "attention"
        : providerWorking
          ? "working"
          : rawProviderWorking && !evidenceLive
            ? "unknown"
            : "quiet";
  const headline = pending
    ? "Needs your response"
    : tone === "unknown"
      ? "Work status unconfirmed"
      : interaction.isManagedLocalSession
        ? display.headline
        : getRuntimeOutcomeLabel(runtime);
  const detail = pending
    ? "A response is required before another message."
    : tone === "unknown"
      ? hostConcern
        ? `Host is ${facts.host.state}; the agent may still be running.`
        : transcriptConcern
          ? "Transcript is lagging the observed session state."
          : `Last reported: ${display.headline}.`
      : display.detail;
  const connection: SessionLedgerState["connection"] = streamConnected
    ? "connected"
    : tone === "unknown" || tone === "attention"
      ? "reconnecting"
      : "recorded";
  const observation = streamConnected
    ? pending
      ? "Updates connected · waiting for your response"
      : hostConcern
        ? `Updates connected · host is ${facts.host.state}`
        : transcriptConcern
          ? "Updates connected · transcript is lagging"
          : tone === "working"
            ? "Updates connected · provider evidence is still valid"
            : "Updates connected · provider work is not asserted"
    : tone === "unknown"
      ? "Updates disconnected · the agent may still be running"
      : "Saved session state · no live updates claimed";
  const primary = facts.presentation.primary;
  const host = facts.host;
  const runtimeMeta = getRuntimeMetaLabel(runtime, nowMs);
  return {
    tone,
    headline,
    detail,
    detailKind: "explanation",
    observation,
    connection,
    animateWork: tone === "working" && providerWorking && streamConnected,
    outputAgeSeconds: null,
    heartbeatAgeMs: activityFeed?.heartbeatAgeMs() ?? null,

    receiptMarks: [],
    facts: [
      {
        label: "Provider evidence",
        value: `${primary?.label ?? "Activity unknown"} · ${facts.activity.state}; valid until ${facts.activity.valid_until ?? "not bounded"}`,
      },
      {
        label: "Host",
        value: `${host.state} · observed ${host.observed_at ?? "not recorded"}`,
      },
      { label: "Transcript", value: facts.transcript.convergence },
      { label: "Control", value: interaction.capabilityLabel },
      ...(runtimeMeta ? [{ label: "Runtime", value: runtimeMeta }] : []),
    ],
  };
}

export function SessionRuntimeStrip({
  session,
  interaction,
  startedLabel,
  variant = "inline",
  testId,
  activityFeed = null,
  streamConnected = false,
}: SessionRuntimeStripProps) {
  const closed = session.session_state.disposition.state === "closed";
  const nowMs = useWallClock(!closed, 1_000);
  const state = buildSessionLedgerState(
    session,
    interaction,
    nowMs,
    streamConnected,
    activityFeed,
  );
  const previousTransitionRef = useRef<{
    sessionId: string;
    tone: SessionLedgerState["tone"];
    resultAt: string | null | undefined;
  } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  useEffect(() => {
    const resultAt = session.session_state.last_result_at;
    const previous = previousTransitionRef.current;
    let nextNotice: string | null = null;
    if (previous && previous.sessionId === session.id) {
      if (resultAt && resultAt !== previous.resultAt) {
        const outcome = session.session_state.last_result_outcome;
        nextNotice = outcome ? `Turn ended · ${outcome}.` : "Turn ended.";
      } else if (
        previous.tone === "unknown" &&
        state.tone === "working" &&
        streamConnected
      ) {
        nextNotice = "Fresh provider evidence restored.";
      }
    }
    previousTransitionRef.current = {
      sessionId: session.id,
      tone: state.tone,
      resultAt,
    };
    setNotice(nextNotice);
  }, [
    session.id,
    session.session_state.last_result_at,
    session.session_state.last_result_outcome,
    state.tone,
    streamConnected,
  ]);
  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(null), 4_000);
    return () => window.clearTimeout(timer);
  }, [notice]);
  return (
    <div
      className={`session-runtime-strip session-runtime-strip--${variant} session-runtime-strip--tone-${state.tone}`}
      data-testid={testId}
      data-strip-tone={
        state.tone === "working"
          ? "live"
          : state.tone === "attention"
            ? "attention"
            : "idle"
      }
      data-stream-connected={streamConnected ? "true" : "false"}
      data-started-label={startedLabel ?? undefined}
    >
      <SessionLedger
        state={state}
        surface="ledger"
        notice={notice}
        activityFeed={activityFeed}
        testId="live-work-ribbon"
      />
    </div>
  );
}
