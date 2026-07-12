/**
 * SessionDetailPage - Single-column session workspace.
 *
 * Layout:
 * - Header: title, live status, info button, archive
 * - Body: transcript fills viewport
 * - Dock: slim runtime strip + composer (Loop Mode picker inline) sticky at bottom
 * - Drawer (overlay): session context (metadata, branches, summary, attach debug)
 * - Telemetry panel only renders with ?debug=telemetry
 */

import { useCallback, useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  Navigate,
  useLocation,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";
import { toast } from "react-hot-toast";
import { Button, EmptyState, Spinner } from "../components/ui";
import { PlayIcon, TrashIcon } from "../components/icons";
import { SessionChat, type SessionChatTarget } from "../components/SessionChat";
import { SessionContextPane } from "../components/session-workspace/SessionContextPane";
import { SessionInfoDrawer } from "../components/session-workspace/SessionInfoDrawer";
import { LoopModePill } from "../components/session-workspace/LoopModePill";
import { RenderTelemetryPanel } from "../components/session-workspace/RenderTelemetryPanel";
import { SessionPauseRequestPanel } from "../components/session-workspace/SessionPauseRequestPanel";
import { SessionRuntimeStrip } from "../components/session-workspace/SessionRuntimeStrip";
import { isSessionClosed, resolveSessionRuntimeState } from "../lib/sessionRuntime";
import { TimelinePane } from "../components/session-workspace/TimelinePane";
import { useLoopModeChange } from "../hooks/useLoopModeChange";
import { useSecondClock } from "../hooks/useSecondClock";
import { useSessionWorkspace } from "../hooks/useSessionWorkspace";
import { useAuth } from "../lib/auth";
import { config } from "../lib/config";
import { useReadinessFlag } from "../lib/readiness-contract";
import { getRuntimeElapsedLabel } from "../lib/sessionTiming";
import { getSessionCardText } from "../lib/sessionUtils";
import { buildSessionShareUrl, copyToClipboard } from "../lib/clipboard";
import {
  createSessionShare,
  respondToPauseRequest,
  setSessionAction,
  type AgentEventId,
  type PauseRequestResponseRequest,
} from "../services/api/agents";
import { ApiError, DEMO_READ_ONLY_MESSAGE } from "../services/api/base";
import {
  continueRemoteSession,
  type LaunchState,
  type RemoteSessionLaunchResponse,
} from "../services/api/launch";
import { getSessionInteractionCapabilities } from "../lib/sessionWorkspace";
import "../styles/session-workspace.css";

function formatContinueFailure(result: RemoteSessionLaunchResponse): string {
  const prefix = result.launch_error_code ? `${result.launch_error_code}: ` : "";
  return `${prefix}${result.launch_error_message || "The machine did not continue this session."}`;
}

type LocalContinueLaunchState = {
  sessionId: string;
  state: LaunchState;
};
function SessionDetailWorkspaceRoute({
  highlightEventId,
  returnTo,
  sessionId,
  debugTelemetry,
  sharedByUserId,
  shareToken,
}: {
  highlightEventId: AgentEventId | null;
  returnTo: string;
  sessionId: string | null;
  debugTelemetry: boolean;
  sharedByUserId: number | null;
  shareToken: string | null;
}) {
  const navigate = useNavigate();
  const { user: currentUser } = useAuth();
  const workspace = useSessionWorkspace(sessionId, {
    highlightEventId,
    shared_by: sharedByUserId,
    share_token: shareToken,
  });

  const {
    session,
    sessionLoading,
    sessionError,
    turns,
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
    fetchPreviousPage,
    hasPreviousPage,
    isFetchingPreviousPage,
    abandonedEvents,
    selectedKey,
    selectKey,
    handleVisibleSelectionChange,
    registerTimelineList,
  } = workspace;
  const nowMs = useSecondClock(Boolean(session && !isSessionClosed(session)));
  const runtimeElapsedLabel = useMemo(
    () => getRuntimeElapsedLabel(session, turns, nowMs),
    [session, turns, nowMs],
  );
  const [drawerOpen, setDrawerOpen] = useState(false);

  const navigateToSession = (nextSessionId: string) => {
    navigate(`/timeline/${nextSessionId}`, {
      replace: nextSessionId === session?.id,
      state: { from: returnTo },
    });
  };

  const handleBack = useCallback(() => {
    navigate(returnTo);
  }, [navigate, returnTo]);
  const { effectiveLoopMode, loopModePending, handleLoopModeChange } =
    useLoopModeChange(session);
  const queryClient = useQueryClient();
  const [confirmingArchive, setConfirmingArchive] = useState(false);
  const [continuingSession, setContinuingSession] = useState(false);
  const [copyingShareLink, setCopyingShareLink] = useState(false);
  const [continueLaunchState, setContinueLaunchState] =
    useState<LocalContinueLaunchState | null>(null);

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

  const handleCopyShareLink = useCallback(async () => {
    if (!session) return;
    const currentUserId = currentUser?.id ?? null;
    if (currentUserId === null || currentUserId === undefined) {
      return;
    }
    if (config.demoMode) {
      toast(DEMO_READ_ONLY_MESSAGE);
      return;
    }
    setCopyingShareLink(true);
    try {
      const share = await createSessionShare(session.id, {});
      const url = buildSessionShareUrl(window.location.origin, share.share_url || share.token);
      const ok = await copyToClipboard(url);
      if (ok) {
        toast.success("Link copied");
      } else {
        toast.error("Couldn't copy link — copy it from the address bar");
      }
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Couldn't create share link");
    } finally {
      setCopyingShareLink(false);
    }
  }, [session, currentUser]);

  const continuationSession = currentThreadSession || session;
  const continueTarget =
    continuationSession?.capabilities?.continue_targets?.[0] ?? null;
  const canContinueSession = Boolean(
    continuationSession?.capabilities?.can_continue &&
      continueTarget &&
      !continuationSession.capabilities?.can_send_input,
  );
  // Adopting an unmanaged/raw transcript starts a fresh managed process — be
  // honest about that vs. re-launching an already-managed session.
  const isAdoptUnmanaged = continueTarget?.adoption_mode === "adopt_unmanaged";
  const continueIdleLabel = isAdoptUnmanaged ? "Continue in Longhouse" : "Continue";
  const continueTitle = isAdoptUnmanaged
    ? "Starts a fresh managed Longhouse process from this transcript"
    : "Continue session";
  const sessionLaunchState = session?.launch_state ?? null;
  const localContinueLaunchState =
    continueLaunchState && continueLaunchState.sessionId === continuationSession?.id
      ? continueLaunchState.state
      : null;
  const effectiveLaunchState =
    sessionLaunchState && sessionLaunchState !== "launching" && sessionLaunchState !== "launching_unknown"
      ? sessionLaunchState
      : (localContinueLaunchState ?? sessionLaunchState);
  const continueLaunchInProgress =
    effectiveLaunchState === "launching" || effectiveLaunchState === "launching_unknown";

  const refreshSessionQueries = useCallback(
    (targetSessionId: string) => {
      queryClient.invalidateQueries({ queryKey: ["agent-session-workspace", targetSessionId] });
      queryClient.invalidateQueries({ queryKey: ["agent-session", targetSessionId] });
      queryClient.invalidateQueries({ queryKey: ["agent-session-thread", targetSessionId] });
      queryClient.invalidateQueries({ queryKey: ["agent-sessions"] });
    },
    [queryClient],
  );

  const handleContinueSession = useCallback(async () => {
    const sessionToContinue = continuationSession;
    if (!canContinueSession || !continueTarget || !sessionToContinue || continuingSession || continueLaunchInProgress) return;
    if (config.demoMode) {
      toast(DEMO_READ_ONLY_MESSAGE);
      return;
    }
    setContinuingSession(true);
    try {
      const result = await continueRemoteSession(sessionToContinue.id, {
        device_id: continueTarget.device_id || undefined,
        cwd: continueTarget.cwd || undefined,
        client_request_id: `continue-${crypto.randomUUID()}`,
      });
      refreshSessionQueries(sessionToContinue.id);
      if (result.session_id !== sessionToContinue.id) {
        refreshSessionQueries(result.session_id);
      }
      if (result.launch_state === "launch_failed" || result.launch_state === "launch_orphaned") {
        setContinueLaunchState(null);
        toast.error(formatContinueFailure(result));
      } else if (result.launch_state === "live") {
        setContinueLaunchState(null);
        toast.success("Session continued");
      } else {
        setContinueLaunchState({
          sessionId: sessionToContinue.id,
          state: result.launch_state,
        });
        toast("Continuing session");
      }
    } catch (err) {
      setContinueLaunchState(null);
      toast.error(err instanceof ApiError ? err.message : "Failed to continue session");
    } finally {
      setContinuingSession(false);
    }
  }, [
    canContinueSession,
    continuationSession,
    continueLaunchInProgress,
    continueTarget,
    continuingSession,
    refreshSessionQueries,
  ]);

  const workspaceReady = !sessionLoading && !eventsLoading;

  useReadinessFlag({
    ready: workspaceReady,
    screenshotReady: workspaceReady,
  });

  if (sessionLoading) {
    return (
      <div className="session-workspace-route session-workspace-route--empty">
        <EmptyState
          icon={<Spinner size="lg" />}
          title="Loading session..."
          description="Fetching session details."
        />
      </div>
    );
  }

  if (sessionError || !session) {
    return (
      <div className="session-workspace-route session-workspace-route--empty">
        <EmptyState
          variant="error"
          title="Error loading session"
          description={
            sessionError instanceof Error
              ? sessionError.message
              : "Session not found or failed to load."
          }
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
  const displaySession =
    effectiveLoopMode === session.loop_mode
      ? session
      : { ...session, loop_mode: effectiveLoopMode };

  // Shared-by pill render conditions and the copy-link button availability.
  // These depend on `displaySession` (declared just above) and the current viewer.
  const sessionSharer = displaySession.sharer ?? null;
  const currentUserId = currentUser?.id ?? null;
  // Defense in depth: the server already hides self-share, but if the cached
  // session response ever disagrees with the current viewer (e.g. a stale
  // query after logout/login in another tab), still skip the pill.
  const shouldShowSharedByPill =
    sessionSharer !== null &&
    sessionSharer !== undefined &&
    (currentUserId === null || sessionSharer.id !== currentUserId);
  const shouldShowCopyLinkButton = currentUserId !== null && currentUserId !== undefined;
  const sharedByDisplayName = sessionSharer?.display_name?.trim() || "a teammate";

  const branchSourceSession = currentThreadSession || session;
  const interaction = getSessionInteractionCapabilities({
    session: branchSourceSession,
    isViewingHead,
    headThreadSession,
  });
  const activePauseRequest =
    branchSourceSession.session_state.pending_interaction != null &&
    branchSourceSession.runtime_display?.pause_request?.status === "pending"
      ? branchSourceSession.runtime_display.pause_request
      : null;
  const composerDisabledReason = activePauseRequest
    ? activePauseRequest.can_respond
      ? "Answer the provider question above before sending another prompt."
      : "Answer the provider question in the terminal before sending another prompt."
    : interaction.composerDisabledReason;

  const sessionChatTarget: SessionChatTarget = {
    id: branchSourceSession.id,
    project: branchSourceSession.project,
    provider: branchSourceSession.provider,
  };
  const runtimeHostLabel =
    displaySession.control?.source_runner_name?.trim() ||
    interaction.sourceOriginLabel ||
    displaySession.home_label ||
    "host";
  const runtime = resolveSessionRuntimeState(displaySession);
  const sessionEnded = Boolean(session && isSessionClosed(session));
  const workspaceClassName = [
    "session-workspace-route",
    "session-workspace-route--single-column",
    `session-workspace-route--tone-${runtime.tone}`,
    interaction.isManagedLocalSession
      ? "session-workspace-route--managed"
      : "session-workspace-route--unmanaged",
  ].join(" ");

  const launchPendingBanner = (() => {
    const state = effectiveLaunchState;
    if (state === "launching" || state === "launching_unknown") {
      return (
        <div className="launch-pending-banner" role="status" data-testid="launch-pending-banner">
          <Spinner size="sm" />
          <span>
            Starting session on {runtimeHostLabel}…{" "}
            {state === "launching_unknown" ? "waiting for the machine to confirm." : ""}
          </span>
        </div>
      );
    }
    if (state === "launch_failed" || state === "launch_orphaned") {
      return (
        <div className="launch-failed-banner" role="alert" data-testid="launch-failed-banner">
          <strong>Launch failed</strong>
          <span>
            {session.launch_error_code ? `${session.launch_error_code}: ` : ""}
            {session.launch_error_message || "The machine did not start this session."}
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
        {shouldShowSharedByPill ? (
          <span
            data-testid="session-shared-by-pill"
            className="session-shared-by-pill"
            title={`Shared by ${sharedByDisplayName}`}
          >
            <span className="session-shared-by-pill__label">Shared by</span>
            <span className="session-shared-by-pill__name">{sharedByDisplayName}</span>
          </span>
        ) : null}
      </div>
    </div>
  );

  const headerRight = (
    <div className="session-workspace-header__actions">
      {canContinueSession ? (
        <Button
          variant="primary"
          size="sm"
          onClick={() => void handleContinueSession()}
          disabled={continuingSession || continueLaunchInProgress}
          title={continueTitle}
          aria-label={continueTitle}
          data-testid="session-continue-button"
        >
          {continuingSession || continueLaunchInProgress ? <Spinner size="sm" /> : <PlayIcon width={13} height={13} />}
          <span>{continuingSession || continueLaunchInProgress ? "Continuing" : continueIdleLabel}</span>
        </Button>
      ) : null}
      {shouldShowCopyLinkButton ? (
        <Button
          variant="ghost"
          size="sm"
          onClick={() => void handleCopyShareLink()}
          disabled={copyingShareLink}
          title="Copy a link to this session"
          aria-label="Copy link to this session"
          data-testid="session-copy-link-button"
        >
          {copyingShareLink ? "Copying" : "Copy link"}
        </Button>
      ) : null}
      <Button
        variant="ghost"
        size="sm"
        onClick={() => setDrawerOpen(true)}
        title="Session details"
        aria-label="Session details"
        data-testid="session-info-button"
      >
        Info
      </Button>
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
        <Button
          variant="ghost"
          size="sm"
          onClick={() => {
            if (config.demoMode) {
              toast(DEMO_READ_ONLY_MESSAGE);
              return;
            }
            setConfirmingArchive(true);
          }}
          title="Archive session"
          aria-label="Archive session"
        >
          <TrashIcon width={13} height={13} />
        </Button>
      )}
    </div>
  );

  // Proactive operator mode (Loop Mode: Assist/Autopilot) is frozen for launch
  // per VISION ("proactive operator mode" under Frozen/removed). The Autopilot
  // value is currently inert, so a visible toggle would imply behavior that does
  // not exist. Keep the component + handlers wired but do not surface the pill
  // until the capability ships and VISION changes. Demo mode may still show it.
  const showLoopModePill =
    config.demoMode && interaction.isManagedLocalSession && !sessionEnded;

  const handlePauseRequestResponse = async (body: PauseRequestResponseRequest) => {
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
    toast.success(result.status === "rejected" ? "Question cancelled" : "Answer sent");
  };

  return (
    <div
      className={workspaceClassName}
      data-control-path={interaction.isManagedLocalSession ? "managed" : "unmanaged"}
      data-runtime-tone={runtime.tone}
    >
      {launchPendingBanner}
      <div className="session-workspace-shell">
        <TimelinePane
          items={items}
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
          selectedKey={selectedKey}
          onSelectKey={selectKey}
          onVisibleSelectionChange={handleVisibleSelectionChange}
          headerLeft={headerLeft}
          headerRight={headerRight}
          renderMedia={!shareToken}
          listRef={registerTimelineList}
          dock={
            <div
              className="session-control-dock session-control-dock--bar"
              data-testid="session-control-dock"
            >
              <SessionRuntimeStrip
                session={displaySession}
                interaction={interaction}
                elapsedLabel={runtimeElapsedLabel}
                variant="bar"
                testId="session-control-strip"
              />
              <div className="session-control-dock__composer">
                {activePauseRequest ? (
                  <SessionPauseRequestPanel
                    pauseRequest={activePauseRequest}
                    onRespond={handlePauseRequestResponse}
                  />
                ) : null}
                <SessionChat
                  key={`${sessionChatTarget.id}:${interaction.mode}`}
                  session={sessionChatTarget}
                  layout="dock"
                  dockHeaderStyle="hidden"
                  chatMode={
                    interaction.mode === "managed_local"
                      ? "managed_local"
                      : undefined
                  }
                  composerPlaceholder={interaction.placeholder}
                  composerDisabledReason={composerDisabledReason}
                  managedLaunchSuggestion={null}
                  submitLabel={interaction.submitLabel}
                  canQueueNextInput={Boolean(
                    displaySession.capabilities?.can_queue_next_input,
                  )}
                  canSteerActiveTurn={Boolean(
                    displaySession.capabilities?.can_steer_active_turn,
                  )}
                  timelineItems={items}
                  isStalled={displaySession.session_state.activity.state === "stalled"}
                  isSessionExecuting={
                    displaySession.session_state.activity.state === "thinking" ||
                    displaySession.session_state.activity.state === "executing"
                  }
                  onSessionChanged={(nextSessionId) => {
                    if (!nextSessionId || nextSessionId === session.id) return;
                    navigate(`/timeline/${nextSessionId}`, {
                      replace: true,
                      state: { from: returnTo },
                    });
                  }}
                />
                {showLoopModePill ? (
                  <LoopModePill
                    currentMode={effectiveLoopMode}
                    pending={loopModePending}
                    onChange={handleLoopModeChange}
                  />
                ) : null}
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
  const shareToken = searchParams.get("share_token") || null;
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
  return (
    <SessionDetailWorkspaceRoute
      key={sessionId ?? "__missing-session__"}
      sessionId={sessionId ?? null}
      highlightEventId={highlightEventId}
      returnTo={returnTo}
      debugTelemetry={debugTelemetry}
      sharedByUserId={sharedByUserId}
      shareToken={shareToken}
    />
  );
}
