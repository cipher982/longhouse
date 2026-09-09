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
  testId?: string;
  /** Per-frame stream feed; absent when the caller has no live stream. */
  activityFeed?: SessionActivityFeed | null;
  /** Viewer subscription state from the existing workspace stream. */
  streamConnected?: boolean;
}

type ProviderEvidence = Pick<SessionLedgerState, "tone"> & {
  sessionId: string;
  resultAt: string | null | undefined;
  streamConnected: boolean;
  providerEvidenceIdentity: string | null;
  preInterruptionEvidenceIdentity: string | null;
  hadObservedWork: boolean;
  interrupted: boolean;
};

/**
 * The provider observation is deliberately narrower than the stream frame:
 * heartbeats and connection handshakes carry no provider activity identity.
 * `valid_until` is omitted because renewing a window is not new evidence.
 */
export function providerEvidenceIdentity(
  activity:
    | {
        state?: string | null;
        tool?: string | null;
        source?: string | null;
        observed_at?: string | null;
      }
    | null
    | undefined,
): string | null {
  if (!activity?.observed_at) return null;
  return JSON.stringify([
    activity.observed_at,
    activity.state ?? null,
    activity.tool ?? null,
    activity.source ?? null,
  ]);
}

export type ProviderEvidenceNoticeAction = "show" | "clear" | "retain";

export interface ProviderEvidenceTransition {
  snapshot: ProviderEvidence;
  notice: string | null;
  noticeAction: ProviderEvidenceNoticeAction;
}

export function advanceProviderEvidenceTransition(
  previous: ProviderEvidence | null,
  current: ProviderEvidence,
  outcome: string | null | undefined,
): ProviderEvidenceTransition {
  if (!previous || previous.sessionId !== current.sessionId) {
    return {
      snapshot: {
        ...current,
        preInterruptionEvidenceIdentity: current.providerEvidenceIdentity,
        hadObservedWork: current.tone === "working",
        interrupted: false,
      },
      notice: null,
      noticeAction: "clear",
    };
  }
  if (
    current.tone === "quiet" &&
    current.resultAt &&
    current.resultAt !== previous.resultAt
  ) {
    return {
      snapshot: {
        ...current,
        preInterruptionEvidenceIdentity: current.providerEvidenceIdentity,
        hadObservedWork: false,
        interrupted: false,
      },
      notice: outcome ? `Turn ended · ${outcome}.` : "Turn ended.",
      noticeAction: "show",
    };
  }

  const hadObservedWork =
    previous.hadObservedWork || current.tone === "working";
  const interrupted =
    previous.interrupted ||
    (hadObservedWork &&
      (!current.streamConnected || current.tone === "unknown"));
  const preInterruptionEvidenceIdentity = previous.interrupted
    ? previous.preInterruptionEvidenceIdentity
    : interrupted
      ? previous.providerEvidenceIdentity
      : current.providerEvidenceIdentity;
  const refreshed =
    interrupted &&
    hadObservedWork &&
    current.tone === "working" &&
    current.streamConnected &&
    preInterruptionEvidenceIdentity !== null &&
    current.providerEvidenceIdentity !== null &&
    current.providerEvidenceIdentity !== preInterruptionEvidenceIdentity;
  const snapshot = {
    ...current,
    preInterruptionEvidenceIdentity,
    hadObservedWork,
    interrupted: refreshed ? false : interrupted,
  };
  if (refreshed) {
    return {
      snapshot,
      notice: "Fresh provider evidence restored.",
      noticeAction: "show",
    };
  }

  const shouldClear =
    previous.tone !== current.tone ||
    previous.resultAt !== current.resultAt ||
    previous.streamConnected !== current.streamConnected ||
    current.tone === "unknown" ||
    current.tone === "attention";
  return {
    snapshot,
    notice: null,
    noticeAction: shouldClear ? "clear" : "retain",
  };
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
      ? "Activity uncertain"
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
          : "Provider activity is unconfirmed."
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
            : "Updates connected · provider activity is unconfirmed"
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
  const runtimeEvidence =
    resolveSessionRuntimeState(session).stateFacts.activity;
  const currentTransition: ProviderEvidence = {
    sessionId: session.id,
    tone: state.tone,
    resultAt: session.session_state.last_result_at,
    streamConnected,
    providerEvidenceIdentity: providerEvidenceIdentity(runtimeEvidence),
    preInterruptionEvidenceIdentity: null,
    hadObservedWork: false,
    interrupted: false,
  };
  const previousTransitionRef = useRef<ProviderEvidence | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  useEffect(() => {
    const transition = advanceProviderEvidenceTransition(
      previousTransitionRef.current,
      currentTransition,
      session.session_state.last_result_outcome,
    );
    previousTransitionRef.current = transition.snapshot;
    if (transition.noticeAction === "show") {
      setNotice(transition.notice);
    } else if (transition.noticeAction === "clear") {
      setNotice(null);
    }
  }, [
    currentTransition.providerEvidenceIdentity,
    currentTransition.resultAt,
    currentTransition.sessionId,
    currentTransition.streamConnected,
    currentTransition.tone,
    session.session_state.last_result_outcome,
  ]);
  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(null), 4_000);
    return () => window.clearTimeout(timer);
  }, [notice]);
  return (
    <div
      className={`session-runtime-strip session-runtime-strip--tone-${state.tone}`}
      data-testid={testId}
      data-strip-tone={
        state.tone === "working"
          ? "live"
          : state.tone === "attention"
            ? "attention"
            : "idle"
      }
      data-stream-connected={streamConnected ? "true" : "false"}
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
