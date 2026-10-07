/**
 * SessionDetailPage - Single-column session workspace.
 *
 * Layout:
 * - Header: back, title + identity subtitle, overflow menu
 * - Body: transcript fills viewport
 * - Dock: runtime strip (activity strip, elapsed, tail) + composer sticky at bottom
 * - Drawer (overlay): session context (metadata, branches, summary, attach debug)
 * - Telemetry panel only renders with ?debug=telemetry
 */

import { useCallback, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { useQueryClient } from "@tanstack/react-query";
import {
  Navigate,
  useLocation,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router";
import { toast } from "react-hot-toast";
import { Button, EmptyState, Spinner } from "@/shared/ui";
import { SessionChat, type SessionChatTarget } from "./chat/SessionChat";
import { ProviderGlyph } from "@/shared/ui/ProviderGlyph";
import { SessionContextPane } from "./SessionContextPane";
import { SessionInfoDrawer } from "./SessionInfoDrawer";
import { SessionOverflowMenu } from "./SessionOverflowMenu";
import { RenderTelemetryPanel } from "./RenderTelemetryPanel";
import { SessionPauseRequestPanel } from "./SessionPauseRequestPanel";
import { SessionRuntimeStrip } from "./SessionRuntimeStrip";
import {
  isUnexpectedResumeStop,
  ResumeSessionModal,
} from "./ResumeSessionModal";
import { BranchSessionCard, branchUnavailableNote } from "./BranchSessionCard";
import { deriveTurnOutline, TurnOutline, turnRowId, type TurnOutlineTurn } from "./TurnOutline";
import { useActiveTurn } from "./useActiveTurn";
import { SessionStateBadge } from "./SessionStateBadge";
import { SessionOpening } from "./SessionOpening";
import {
  buildSessionMetaItems,
  getSessionHeaderState,
} from "./sessionHeaderState";
import { Sparkline } from "@/shared/instruments/Sparkline";
import { ReadoutRail } from "@/shared/instruments/ReadoutRail";
import {
  bucketToolActivityByMinute,
  countToolCallsThisTurn,
  findRunningTool,
  getRunningTurnStartMs,
} from "@/shared/instruments/toolActivity";
import {
  isSessionClosed,
  resolveSessionRuntimeState,
} from "@/shared/session/sessionRuntime";
import { TimelinePane } from "./TimelinePane";
import { useWallClock } from "@/shared/hooks/useWallClock";
import { useSessionWorkspace } from "./useSessionWorkspace";
import { useAuth } from "@/features/auth/auth";
import { useHeaderSlot } from "@/app/headerSlot";
import { useStoredState } from "@/shared/hooks/useStoredState";
import { GaugeIcon } from "@/shared/ui/icons";
import { SessionRailFrame } from "./rail/SessionRail";
import { DisplaySettingsPopover } from "./display/DisplaySettingsPopover";
import { displaySettingsStyle } from "./display/displaySettings";
import { useDisplaySettings } from "./display/useDisplaySettings";
import { useReportActiveSession, useSessionRail } from "./rail/sessionRailContext";
import { config } from "@/shared/lib/config";
import { useReadinessFlag } from "@/shared/lib/readiness-contract";
import { getSessionStartedLabel } from "./sessionTiming";
import { getSessionCardText } from "@/shared/session/sessionLabels";
import { useMarkSessionRead } from "./useMarkSessionRead";
import {
  createSessionResumeIntent,
  respondToPauseRequest,
  setSessionAction,
  setSessionTimelineVisibility,
  type AgentEventId,
  type PauseRequestResponseRequest,
  type SessionResumeIntent,
} from "@/shared/api/agents";
import { errorDetails } from "@/shared/ui/errorDetails";
import { ApiError, DEMO_READ_ONLY_MESSAGE } from "@/shared/api/base";
import {
  countTimelineItems,
  getSessionInteractionCapabilities,
} from "@/shared/session/model";
import type { OutboxEntry } from "./OutboxRow";
import "./session-workspace.css";

const GENERIC_HOME_LABELS = new Set([
  "On this Mac",
  "Hosted",
  "Moved to cloud",
  "This machine",
]);

function SessionDetailWorkspaceRoute({
  highlightEventId,
  returnTo,
  sessionId,
  debugTelemetry,
  sharedByUserId,
}: {
  highlightEventId: AgentEventId | null;
  returnTo: string;
  sessionId: string | null;
  debugTelemetry: boolean;
  sharedByUserId: number | null;
}) {
  const navigate = useNavigate();
  const { user: currentUser } = useAuth();
  const headerSlot = useHeaderSlot();
  const sessionRail = useSessionRail();
  // The readout panel (turn clock, context, tool calls, activity) is opt-in:
  // the header already carries the counts and state it used to repeat.
  const [readoutsOpen, setReadoutsOpen] = useStoredState<boolean>(
    "longhouse.session.readouts",
    false,
    (raw) => (typeof raw === "boolean" ? raw : null),
  );
  const display = useDisplaySettings();
  const displayStyle = useMemo(() => displaySettingsStyle(display.settings), [display.settings]);
  const workspace = useSessionWorkspace(sessionId, {
    highlightEventId,
    shared_by: sharedByUserId,
  });

  const {
    session,
    sessionLoading,
    sessionError,
    threadSessions,
    currentThreadSession,
    headThreadSession,
    isViewingHead,
    showAbandonedBranches,
    setShowAbandonedBranches,
    totalEntries,
    loadedEntryCount,
    items,
    eventsLoading,
    eventsError,
    controlOnly,
    fetchPreviousPage,
    hasPreviousPage,
    isFetchingPreviousPage,
    abandonedEvents,
    selectedKey,
    selectKey,
    handleVisibleSelectionChange,
    registerTimelineList,
    streamConnected,
    activityFeed,
  } = workspace;
  const nowMs = useWallClock(Boolean(session && !isSessionClosed(session)));
  const transcriptCounts = useMemo(() => countTimelineItems(items), [items]);
  // Phase 4 (Instruments): sparkline + readout-rail counts derived from
  // `items`. Hoisted above the sessionLoading/!session early returns below
  // (not alongside the rest of the rail wiring near workspaceClassName)
  // because a hook may never run conditionally — those earlier returns
  // would otherwise skip these useMemo calls on the loading render and
  // trip "Rendered more hooks than during the previous render" once the
  // session loads.
  const headerActivityBuckets = useMemo(
    () => bucketToolActivityByMinute(items, nowMs, 30),
    [items, nowMs],
  );
  // null (not 0) when there are no tool calls this turn, so the readout
  // rail omits the row entirely on an empty thread rather than showing "0".
  const toolCallsThisTurn = useMemo(() => {
    const count = countToolCallsThisTurn(items);
    return count > 0 ? count : null;
  }, [items]);
  const waitingOn = useMemo(() => findRunningTool(items), [items]);
  // The one turn-elapsed anchor shared by the header, the composer clock
  // (SessionChat's own copy of this derivation), and the readout rail's Turn
  // readout below — see getRunningTurnStartMs for why this replaces
  // activity.observed_at.
  const turnStartMs = useMemo(() => getRunningTurnStartMs(items), [items]);
  // Item 7: the turn outline column. Turns derive from the same loaded
  // thread as everything else on this page — no new fetch.
  const turns = useMemo(() => deriveTurnOutline(items), [items]);
  // The outline follows the reader: the turn in view (scroll-spy over the
  // transcript list), or the one they just clicked until they scroll again.
  const [timelineList, setTimelineList] = useState<HTMLDivElement | null>(null);
  const attachTimelineList = useCallback(
    (node: HTMLDivElement | null) => {
      setTimelineList(node);
      registerTimelineList(node);
    },
    [registerTimelineList],
  );
  const { activeKey: activeTurnKey, pin: pinTurn } = useActiveTurn(timelineList);
  const handleSelectTurn = useCallback((turn: TurnOutlineTurn) => {
    selectKey(`message:${turn.eventId}`);
    const row = document.getElementById(turnRowId(turn.eventId));
    if (!row) return;
    pinTurn(turn.eventId);
    row.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [selectKey, pinTurn]);

  // Read-on-open acknowledgement for Console results; shared viewers never
  // acknowledge (console-unread-acknowledgement spec).
  useMarkSessionRead({
    sessionId,
    sessionState: session?.session_state,
    disabled: sharedByUserId != null,
  });
  const sessionStartedLabel = useMemo(
    () => getSessionStartedLabel(session, nowMs),
    [session, nowMs],
  );
  const [drawerOpen, setDrawerOpen] = useState(false);
  // Sends the composer owns but the transcript renders at its tail.
  const [outboxEntries, setOutboxEntries] = useState<OutboxEntry[]>([]);

  const navigateToSession = (nextSessionId: string) => {
    navigate(`/timeline/${nextSessionId}`, {
      replace: nextSessionId === session?.id,
      state: { from: returnTo },
    });
  };

  const handleBack = useCallback(() => {
    navigate(returnTo);
  }, [navigate, returnTo]);
  const queryClient = useQueryClient();
  const [confirmingArchive, setConfirmingArchive] = useState(false);
  const [hidingSession, setHidingSession] = useState(false);
  const [resumeLoading, setResumeLoading] = useState(false);
  const [resumeIntent, setResumeIntent] = useState<SessionResumeIntent | null>(
    null,
  );

  const handleArchiveConfirm = useCallback(async () => {
    if (!session) return;
    setConfirmingArchive(false);
    if (config.demoMode) {
      toast(DEMO_READ_ONLY_MESSAGE);
      return;
    }
    try {
      await setSessionAction(session.id, "archive");
      queryClient.invalidateQueries({ queryKey: ["agent-sessions"] });
      queryClient.invalidateQueries({
        queryKey: ["agent-session", session.id],
      });
      handleBack();
    } catch {
      toast.error("Failed to archive session");
    }
  }, [session, queryClient, handleBack]);

  const handleTimelineVisibility = useCallback(async () => {
    if (!session || hidingSession) return;
    if (config.demoMode) {
      toast(DEMO_READ_ONLY_MESSAGE);
      return;
    }
    setHidingSession(true);
    try {
      const hidden = !session.user_hidden_from_timeline;
      await setSessionTimelineVisibility(session.id, hidden);
      queryClient.invalidateQueries({ queryKey: ["agent-sessions"] });
      queryClient.invalidateQueries({
        queryKey: ["agent-session", session.id],
      });
      toast.success(
        hidden
          ? "Session hidden from timeline"
          : "Session restored to timeline",
      );
      if (hidden) handleBack();
    } catch {
      toast.error("Failed to update timeline visibility");
    } finally {
      setHidingSession(false);
    }
  }, [session, hidingSession, queryClient, handleBack]);

  const handleResume = useCallback(async () => {
    if (!session || resumeLoading) return;
    setResumeLoading(true);
    try {
      setResumeIntent(await createSessionResumeIntent(session.id));
    } catch (error) {
      toast.error(
        error instanceof ApiError ? error.message : "Couldn't prepare Resume",
      );
    } finally {
      setResumeLoading(false);
    }
  }, [session, resumeLoading]);

  // Read the canonical launch fact, not the top-level compat alias. Two
  // spellings of one readiness row is what let this banner and the interaction
  // reducer disagree about whether a starting session was starting.
  const launchFacts = session?.session_state?.launch ?? null;

  const refreshSessionQueries = useCallback(
    (targetSessionId: string) => {
      queryClient.invalidateQueries({
        queryKey: ["agent-session-workspace", targetSessionId],
      });
      queryClient.invalidateQueries({
        queryKey: ["agent-session", targetSessionId],
      });
      queryClient.invalidateQueries({
        queryKey: ["agent-session-thread", targetSessionId],
      });
      queryClient.invalidateQueries({ queryKey: ["agent-sessions"] });
    },
    [queryClient],
  );

  // Tell the rail what this session looks like right now, so it can show it
  // even when it is not among the recent sessions the rail lists.
  const railReport = useMemo(() => {
    if (!session) return null;
    const state = getSessionHeaderState(session, nowMs, turnStartMs);
    return {
      id: session.id,
      title: getSessionCardText(session, { titleMaxChars: 80 }).title,
      provider: session.provider ?? null,
      host: session.control?.source_runner_name?.trim() || session.device_id || null,
      stateText: state.text,
      tone: state.tone,
    };
  }, [session, nowMs, turnStartMs]);
  useReportActiveSession(railReport);

  const workspaceReady = !sessionLoading && !eventsLoading;

  useReadinessFlag({
    ready: workspaceReady,
    screenshotReady: workspaceReady,
  });

  // The frame after the click: the title bar from the Timeline's data and
  // placeholder rows, never a blank page or a lone spinner.
  if (sessionLoading) {
    return <SessionOpening sessionId={sessionId} headerTarget={headerSlot} onBack={handleBack} />;
  }

  // A failed background refresh (a deploy restart answers 502 for a few
  // seconds) keeps the loaded session on screen; only a session that never
  // loaded becomes an error page.
  if (!session) {
    return (
      <div className="session-workspace-route session-workspace-route--empty">
        <EmptyState
          variant="error"
          title="Couldn't open this session"
          description="Longhouse couldn't load this session. It may have been removed, or the server may be restarting."
          details={errorDetails(sessionError)}
          action={
            <Button variant="primary" onClick={handleBack}>
              Back to Timeline
            </Button>
          }
        />
      </div>
    );
  }

  const title = getSessionCardText(session, { titleMaxChars: 96 }).title;
  const displaySession = session;

  // Shared-by pill render conditions. These depend on `displaySession`
  // (declared just above) and the current viewer.
  const sessionSharer = displaySession.sharer ?? null;
  const currentUserId = currentUser?.id ?? null;
  // Defense in depth: the server already hides self-share, but if the cached
  // session response ever disagrees with the current viewer (e.g. a stale
  // query after logout/login in another tab), still skip the pill.
  const shouldShowSharedByPill =
    sessionSharer !== null &&
    sessionSharer !== undefined &&
    (currentUserId === null || sessionSharer.id !== currentUserId);
  const sharedByDisplayName =
    sessionSharer?.display_name?.trim() || "a teammate";

  const branchSourceSession = currentThreadSession || session;
  const interaction = getSessionInteractionCapabilities({
    session: branchSourceSession,
  });
  const activePauseRequest =
    branchSourceSession.session_state.pending_interaction != null &&
    branchSourceSession.runtime_display?.pause_request?.status === "pending"
      ? branchSourceSession.runtime_display.pause_request
      : null;
  const resumeAvailable =
    !activePauseRequest &&
    isViewingHead &&
    branchSourceSession.session_state.control.actions.resume.state ===
      "available";
  const resumeHostLabel =
    branchSourceSession.control?.source_runner_name?.trim() ||
    branchSourceSession.device_id ||
    "the original machine";
  const composerDisabledReason = activePauseRequest
    ? activePauseRequest.can_respond
      ? "Answer the provider question above before sending another prompt."
      : "Answer the provider question in the terminal before sending another prompt."
    : resumeAvailable
      ? `This run has ended. To continue the same conversation, run the resume command in a terminal on ${resumeHostLabel}.`
      : interaction.composerDisabledReason;

  const sessionChatTarget: SessionChatTarget = {
    id: branchSourceSession.id,
    project: branchSourceSession.project,
    provider: branchSourceSession.provider,
    device_id: branchSourceSession.device_id,
    selected_model: branchSourceSession.selected_model,
    capabilities: branchSourceSession.capabilities,
    session_state: branchSourceSession.session_state,
  };
  const runtimeHostLabel =
    displaySession.control?.source_runner_name?.trim() ||
    displaySession.device_id ||
    "the original machine";
  // Who and where, once, under the title. The host is only named when the
  // server actually recorded a machine; home_label can be a phrase such as
  // "On this Mac", and a placeholder would claim a machine. Same order as
  // iOS (sessionIdentityHost): the recorded device before the home label.
  const identityHost =
    displaySession.control?.source_runner_name?.trim() ||
    [displaySession.device_id, displaySession.home_label]
      .map((candidate) => candidate?.trim() || null)
      .find((candidate) => candidate && !GENERIC_HOME_LABELS.has(candidate)) ||
    null;
  const usageLabel = displaySession.usage_latest?.label ?? null;
  const runtime = resolveSessionRuntimeState(displaySession);
  const headerState = getSessionHeaderState(displaySession, nowMs, turnStartMs);
  // Plain items, " · "-joined. Counts prefer the session's own totals so a
  // partly loaded transcript does not shrink them.
  const metaItems = buildSessionMetaItems({
    provider: interaction.providerLabel || null,
    project: displaySession.project?.trim() || null,
    host: identityHost,
    messages: Math.max(
      (displaySession.user_messages ?? 0) + (displaySession.assistant_messages ?? 0),
      transcriptCounts.messages,
    ),
    toolCalls: Math.max(displaySession.tool_calls ?? 0, transcriptCounts.toolCalls),
  });
  const identityLabel = [...metaItems, ...(usageLabel ? [usageLabel] : [])].join(" · ") || null;
  const contextTokens = displaySession.usage_latest?.context_tokens ?? null;
  const contextWindow = displaySession.usage_latest?.context_window ?? null;
  // A ring only when the window is known; otherwise the label's "267k ctx"
  // stands alone and nothing guesses a denominator.
  const contextFraction =
    contextTokens != null && contextWindow != null && contextWindow > 0
      ? Math.min(1, Math.max(0, contextTokens / contextWindow))
      : null;
  // The branch form sits beside Resume in the Run ended notice. A refusal gets
  // words only when `branchUnavailableNote` has some; see there for which.
  const branchAction = branchSourceSession.session_state.control.actions.branch;
  // Served from the live thread edge before a branch has shipped, and from the
  // same edge afterwards, so the relationship is visible for the whole life of
  // the child rather than appearing once its first transcript lands.
  const branchedFromSessionId =
    displaySession.continuation_kind === "fork"
      ? displaySession.continued_from_session_id
      : null;
  const branchAvailable = branchAction?.state === "available";
  const showBranchCard =
    isViewingHead &&
    branchSourceSession.session_state.run?.lifecycle === "ended" &&
    branchSourceSession.session_state.mode === "helm" &&
    (branchAvailable || branchUnavailableNote(branchAction?.reason) !== null);
  // Phase 4 (Instruments): "Turn" readout data — the sparkline/tool-call/
  // waiting-on useMemos live above, before the early returns; this part is
  // plain per-render arithmetic on `displaySession`, not a hook, so it's
  // fine here.
  const turnLive = headerState.tone === "live";
  const lastTurn = turns.length > 0 ? turns[turns.length - 1] : null;
  const runningTurnKey = turnLive ? (lastTurn?.key ?? null) : null;
  const currentTurnKey = activeTurnKey;
  const turnElapsedSeconds =
    turnLive && turnStartMs != null
      ? Math.max(0, Math.floor((nowMs - turnStartMs) / 1_000))
      : null;
  const lastTurnSeconds = displaySession.last_turn
    ? Math.round(displaySession.last_turn.duration_ms / 1_000)
    : null;
  // While running: elapsed so far. Otherwise: the most recently finished
  // turn's real duration, never a fabricated "since idle" value.
  const turnSeconds = turnElapsedSeconds ?? lastTurnSeconds;

  // With the rail, the turns list is an accordion under this session's rail
  // row; without it (shared views, tests) it keeps its own column.
  const turnOutline = (
    <TurnOutline
      turns={turns}
      runningTurnKey={runningTurnKey}
      currentTurnKey={currentTurnKey}
      onSelectTurn={handleSelectTurn}
    />
  );

  const workspaceClassName = [
    "session-workspace-route",
    "session-workspace-route--single-column",
    `session-workspace-route--tone-${runtime.tone}`,
    interaction.isManagedLocalSession
      ? "session-workspace-route--managed"
      : "session-workspace-route--unmanaged",
    readoutsOpen ? "session-workspace-route--readouts" : null,
  ]
    .filter(Boolean)
    .join(" ");

  const launchPendingBanner = (() => {
    const state = launchFacts?.state ?? null;
    if (state === "pending" || state === "dispatched") {
      return (
        <div
          className="launch-pending-banner"
          role="status"
          data-testid="launch-pending-banner"
        >
          <Spinner size="sm" />
          <span>
            Starting session on {runtimeHostLabel}…{" "}
            {state === "dispatched"
              ? "waiting for the machine to confirm."
              : ""}
          </span>
        </div>
      );
    }
    if (state === "failed" || state === "abandoned") {
      return (
        <div
          className="launch-failed-banner"
          role="alert"
          data-testid="launch-failed-banner"
        >
          <strong>Launch failed</strong>
          <span>
            {launchFacts?.error_code ? `${launchFacts.error_code}: ` : ""}
            {launchFacts?.error_message ||
              "The machine did not start this session."}
          </span>
        </div>
      );
    }
    return null;
  })();

  const headerLeft = (
    <div className="session-workspace-header__left">
      <Button
        variant="ghost"
        size="sm"
        onClick={handleBack}
        title="Back to timeline"
        aria-label="Back to timeline"
      >
        &larr;
      </Button>
      <div className="session-workspace-header__title-stack">
        <span className="session-workspace-header__name" title={title}>
          {title}
        </span>
        {identityLabel ? (
          <span
            className="session-workspace-header__identity"
            data-testid="session-identity"
            title={identityLabel}
          >
            {displaySession.provider ? (
              <ProviderGlyph
                provider={displaySession.provider}
                size={13}
                variant="bare"
                className="session-workspace-header__provider-glyph"
              />
            ) : null}
            <span className="session-workspace-header__meta-text">
              {metaItems.join(" · ")}
              {usageLabel ? (
                <span className="session-workspace-header__usage" data-testid="session-usage">
                  {metaItems.length > 0 ? " · " : ""}
                  {contextFraction != null ? (
                    <span
                      className="session-context-ring"
                      data-testid="session-context-ring"
                      style={{ ["--ring-fill" as string]: `${Math.round(contextFraction * 100)}%` }}
                      title={`${Math.round(contextFraction * 100)}% of the context window`}
                      aria-hidden="true"
                    />
                  ) : null}
                  {usageLabel}
                </span>
              ) : null}
            </span>
          </span>
        ) : null}
        {shouldShowSharedByPill ? (
          <span
            data-testid="session-shared-by-pill"
            className="session-shared-by-pill"
            title={`Shared by ${sharedByDisplayName}`}
          >
            <span className="session-shared-by-pill__label">Shared by</span>
            <span className="session-shared-by-pill__name">
              {sharedByDisplayName}
            </span>
          </span>
        ) : null}
        {branchedFromSessionId ? (
          <button
            type="button"
            className="session-branched-from"
            data-testid="session-branched-from"
            onClick={() => navigateToSession(branchedFromSessionId)}
            title="Open the session this branched from"
          >
            Branched from an earlier session
          </button>
        ) : null}
      </div>
    </div>
  );

  const headerRight = (
    <div className="session-workspace-header__actions">
      <DisplaySettingsPopover
        settings={display.settings}
        onChange={display.update}
        onReplace={display.replace}
      />
      <button
        type="button"
        className={`timeline-pane__filter-toggle${readoutsOpen ? " is-active" : ""}`}
        onClick={() => setReadoutsOpen((open) => !open)}
        aria-label={readoutsOpen ? "Hide readouts" : "Show readouts"}
        aria-pressed={readoutsOpen}
        title="Turn clock, context and activity"
        data-testid="session-readouts-toggle"
      >
        <GaugeIcon width={14} height={14} />
      </button>
      {confirmingArchive ? (
        <div className="session-detail-archive-confirm">
          <span className="session-detail-archive-confirm-label">Archive?</span>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setConfirmingArchive(false)}
          >
            Cancel
          </Button>
          <Button
            variant="danger"
            size="sm"
            onClick={() => void handleArchiveConfirm()}
          >
            Archive
          </Button>
        </div>
      ) : (
        <SessionOverflowMenu
          label="Session actions"
          testId="session-overflow-menu"
          items={[
            {
              key: "details",
              label: "Session details",
              testId: "session-info-button",
              opensDialog: true,
              onSelect: () => setDrawerOpen(true),
            },
            {
              key: "visibility",
              label: hidingSession
                ? "Saving…"
                : session.user_hidden_from_timeline
                  ? "Restore to timeline"
                  : "Hide from timeline",
              disabled: hidingSession,
              testId: "session-visibility-button",
              onSelect: () => void handleTimelineVisibility(),
            },
            {
              key: "archive",
              label: "Archive session…",
              danger: true,
              testId: "session-archive-button",
              onSelect: () => {
                if (config.demoMode) {
                  toast(DEMO_READ_ONLY_MESSAGE);
                  return;
                }
                setConfirmingArchive(true);
              },
            },
          ]}
        />
      )}
    </div>
  );

  const handlePauseRequestResponse = async (
    body: PauseRequestResponseRequest,
  ) => {
    if (!activePauseRequest) return;
    if (config.demoMode) {
      throw new Error(DEMO_READ_ONLY_MESSAGE);
    }
    const result = await respondToPauseRequest(
      branchSourceSession.id,
      activePauseRequest.id,
      body,
    );
    refreshSessionQueries(branchSourceSession.id);
    toast.success(
      result.status === "rejected" ? "Question cancelled" : "Answer sent",
    );
  };

  return (
    <div
      className={workspaceClassName}
      style={displayStyle}
      data-session-id={displaySession.id}
      data-state-commit-seq={
        displaySession.session_state.commit_seq ?? undefined
      }
      data-activity-state={displaySession.session_state.activity.state}
      data-activity-observed-at={
        displaySession.session_state.activity.observed_at ?? undefined
      }
      data-control-path={
        interaction.isManagedLocalSession ? "managed" : "unmanaged"
      }
      data-runtime-tone={runtime.tone}
    >
      {launchPendingBanner}
      {sessionRail?.turnsTarget ? createPortal(turnOutline, sessionRail.turnsTarget) : null}
      <div className="session-workspace-shell">
        <TimelinePane
          items={items}
          provider={displaySession.provider}
          outbox={outboxEntries}
          totalEntries={totalEntries}
          loadedEntries={loadedEntryCount}
          abandonedEvents={abandonedEvents}
          showAbandonedBranches={showAbandonedBranches}
          onShowAbandonedBranchesChange={setShowAbandonedBranches}
          hasPreviousPage={hasPreviousPage ?? false}
          isFetchingPreviousPage={isFetchingPreviousPage}
          onFetchPreviousPage={() => void fetchPreviousPage()}
          loading={eventsLoading}
          error={eventsError}
          controlOnly={controlOnly}
          selectedKey={selectedKey}
          onSelectKey={selectKey}
          onVisibleSelectionChange={handleVisibleSelectionChange}
          headerLeft={headerLeft}
          headerTarget={headerSlot}
          headerState={
            <SessionStateBadge
              tone={headerState.tone}
              text={headerState.text}
              ended={displaySession.session_state.disposition.state === "closed"}
              testId="session-header-state"
            />
          }
          headerRight={headerRight}
          outline={sessionRail ? undefined : turnOutline}
          rail={
            readoutsOpen ? (
            <ReadoutRail
              activity={<Sparkline data={headerActivityBuckets} live={turnLive} />}
              turnSeconds={turnSeconds}
              turnLive={turnLive}
              contextTokens={displaySession.usage_latest?.context_tokens ?? null}
              contextWindow={displaySession.usage_latest?.context_window ?? null}
              toolCallsThisTurn={toolCallsThisTurn}
              toolCallsLive={turnLive}
              waitingOn={waitingOn}
            />
            ) : null
          }
          listRef={attachTimelineList}
          dock={
            <div
              className="session-control-dock session-control-dock--bar"
              data-testid="session-control-dock"
            >
              <div
                className="session-balanced-field"
                data-testid="session-balanced-field"
              >
                <div className="session-control-dock__composer">
                  {activePauseRequest ? (
                    <SessionPauseRequestPanel
                      pauseRequest={activePauseRequest}
                      onRespond={handlePauseRequestResponse}
                    />
                  ) : null}
                  <SessionChat
                    key={sessionChatTarget.id}
                    session={sessionChatTarget}
                    layout="dock"
                    chatMode={
                      interaction.mode === "managed_local"
                        ? "managed_local"
                        : undefined
                    }
                    composerPlaceholder={interaction.placeholder}
                    composerDisabledReason={composerDisabledReason}
                    composerDisabledTitle={
                      activePauseRequest
                        ? "Response required"
                        : resumeAvailable
                          ? "Run ended"
                          : interaction.notice?.title ?? null
                    }
                    composerDisabledAction={
                      resumeAvailable || showBranchCard ? (
                        <>
                          {resumeAvailable ? (
                            <Button
                              type="button"
                              variant="primary"
                              size="sm"
                              onClick={() => void handleResume()}
                              disabled={resumeLoading}
                              data-testid="session-resume-button"
                            >
                              {resumeLoading ? "Checking…" : "Show resume command"}
                            </Button>
                          ) : null}
                          {showBranchCard ? (
                            <BranchSessionCard
                              sessionId={branchSourceSession.id}
                              providerLabel={interaction.providerLabel}
                              machineLabel={runtimeHostLabel}
                              available={branchAvailable}
                              unavailableReason={branchAction?.reason}
                              onBranched={navigateToSession}
                            />
                          ) : null}
                        </>
                      ) : null
                    }
                    managedLaunchSuggestion={null}
                    submitLabel={interaction.submitLabel}
                    canQueueNextInput={Boolean(
                      displaySession.capabilities?.can_queue_next_input,
                    )}
                    canSteerActiveTurn={Boolean(
                      displaySession.capabilities?.can_steer_active_turn,
                    )}
                    timelineItems={items}
                    onOutboxChange={setOutboxEntries}
                    composerHeaderAccessory={
                      <SessionRuntimeStrip
                        session={displaySession}
                        interaction={interaction}
                        testId="session-control-strip"
                        activityFeed={activityFeed ?? null}
                        streamConnected={streamConnected}
                        compact
                      />
                    }
                  />
                </div>
              </div>
            </div>
          }
        />
        {debugTelemetry ? (
          <div className="session-workspace-debug">
            <RenderTelemetryPanel sessionId={session.id} />
          </div>
        ) : null}
      </div>
      <SessionInfoDrawer
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        title={title}
      >
        <SessionContextPane
          session={displaySession}
          title={title}
          headThreadSession={headThreadSession}
          threadSessions={threadSessions}
          isViewingHead={isViewingHead}
          onOpenSession={(nextId) => {
            setDrawerOpen(false);
            navigateToSession(nextId);
          }}
          onOpenLatest={() => {
            if (!headThreadSession) return;
            setDrawerOpen(false);
            navigateToSession(headThreadSession.id);
          }}
          continuationNotice={interaction.notice}
          hideHero
        />
      </SessionInfoDrawer>
      {resumeIntent ? (
        <ResumeSessionModal
          intent={resumeIntent}
          unexpectedStop={isUnexpectedResumeStop(
            displaySession.runtime_display?.terminal_reason ??
              displaySession.session_state.disposition.close_reason,
          )}
          onClose={() => setResumeIntent(null)}
        />
      ) : null}
    </div>
  );
}

export default function SessionDetailPage() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const location = useLocation();
  const [searchParams] = useSearchParams();

  const highlightEventId = useMemo(() => {
    const raw = searchParams.get("event_id");
    return raw || null;
  }, [searchParams]);

  const debugTelemetry = searchParams.get("debug") === "telemetry";
  const shouldAutoResume = searchParams.get("resume") === "1";
  const sharedByUserId = useMemo(() => {
    const raw = searchParams.get("shared_by");
    if (!raw) return null;
    const parsed = Number(raw);
    // Server already enforces ge=1; keep the client permissive so a stale
    // param or a manually-typed value does not crash the page.
    if (!Number.isFinite(parsed) || parsed < 1) return null;
    return Math.trunc(parsed);
  }, [searchParams]);
  const returnTo =
    (location.state as { from?: string } | null)?.from ?? "/timeline";

  if (shouldAutoResume) {
    const next = new URLSearchParams(searchParams);
    next.delete("resume");
    return (
      <Navigate
        to={{
          pathname: location.pathname,
          search: next.toString() ? `?${next.toString()}` : "",
        }}
        replace
        state={{ from: returnTo }}
      />
    );
  }

  // Key the workspace by session ID so filters, selection, and scroll state reset
  // through remount semantics instead of a session-sync effect inside the hook.
  const workspaceRoute = (
    <SessionDetailWorkspaceRoute
      key={sessionId ?? "__missing-session__"}
      sessionId={sessionId ?? null}
      highlightEventId={highlightEventId}
      returnTo={returnTo}
      debugTelemetry={debugTelemetry}
      sharedByUserId={sharedByUserId}
    />
  );
  // A shared view is someone else's session; the viewer's own rail does not
  // belong beside it. Everywhere else the rail stays mounted across switches.
  if (sharedByUserId != null) return workspaceRoute;
  return (
    <SessionRailFrame activeSessionId={sessionId ?? null} returnTo={returnTo}>
      {workspaceRoute}
    </SessionRailFrame>
  );
}
