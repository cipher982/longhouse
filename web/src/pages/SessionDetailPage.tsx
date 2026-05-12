/**
 * SessionDetailPage - IDE-style session workspace for one synced transcript.
 *
 * Layout:
 * - Left: session context and branch lineage
 * - Center: event timeline transcript (tool detail expands inline)
 * - Bottom dock: inline live-session composer and session control
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
import { TrashIcon } from "../components/icons";
import { SessionChat, type SessionChatTarget } from "../components/SessionChat";
import { SessionContextPane } from "../components/session-workspace/SessionContextPane";
import { SessionRuntimeStrip } from "../components/session-workspace/SessionRuntimeStrip";
import { isSessionClosed, resolveSessionRuntimeState } from "../lib/sessionRuntime";
import { TimelinePane } from "../components/session-workspace/TimelinePane";
import { WorkspaceShell } from "../components/workspace/WorkspaceShell";
import { useLoopModeChange } from "../hooks/useLoopModeChange";
import { useSecondClock } from "../hooks/useSecondClock";
import { useSessionWorkspace } from "../hooks/useSessionWorkspace";
import { config } from "../lib/config";
import { useReadinessFlag } from "../lib/readiness-contract";
import { getRuntimeElapsedLabel } from "../lib/sessionTiming";
import { setSessionAction } from "../services/api/agents";
import { DEMO_READ_ONLY_MESSAGE } from "../services/api/base";
import {
  getSessionInteractionCapabilities,
  getToolDisplayInfo,
  getToolSummary,
  type TimelineItem,
} from "../lib/sessionWorkspace";
import "../styles/session-workspace.css";

function normalizeRunningToolLabel(label: string): string {
  const lower = label.trim().toLowerCase();
  if (lower === "bash" || lower === "shell" || lower === "terminal") {
    return "shell";
  }
  return label;
}

function getActiveToolDetail(items: TimelineItem[]): string | null {
  for (let index = items.length - 1; index >= 0; index -= 1) {
    const item = items[index];
    if (item.kind !== "tool") continue;
    const { interaction } = item;
    if (interaction.resultEvent || interaction.pairing === "orphan") continue;
    const info = getToolDisplayInfo(interaction.toolName);
    const toolLabel = normalizeRunningToolLabel(info.displayName);
    const summary = getToolSummary(interaction);
    return summary ? `Running ${toolLabel} · ${summary}` : `Running ${toolLabel}`;
  }
  return null;
}

function SessionDetailWorkspaceRoute({
  highlightEventId,
  returnTo,
  sessionId,
}: {
  highlightEventId: number | null;
  returnTo: string;
  sessionId: string | null;
}) {
  const navigate = useNavigate();
  const workspace = useSessionWorkspace(sessionId, { highlightEventId });

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
  // Phase 3 of session-liveness-honesty: close the clock on lifecycle==='closed'
  // when the axis is present; fall back to terminal_state for older payloads.
  const nowMs = useSecondClock(Boolean(session && !isSessionClosed(session)));
  const runtimeElapsedLabel = useMemo(
    () => getRuntimeElapsedLabel(session, turns, nowMs),
    [session, turns, nowMs],
  );
  const activeToolDetail = useMemo(() => getActiveToolDetail(items), [items]);

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

  const title =
    session.summary_title && session.summary_title !== "Untitled Session"
      ? session.summary_title
      : session.project || session.git_branch || "Session";
  const displaySession =
    effectiveLoopMode === session.loop_mode
      ? session
      : { ...session, loop_mode: effectiveLoopMode };

  const branchSourceSession = currentThreadSession || session;
  const interaction = getSessionInteractionCapabilities({
    session: branchSourceSession,
    isViewingHead,
    headThreadSession,
  });

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
  const workspaceClassName = [
    "session-workspace-route",
    `session-workspace-route--tone-${runtime.tone}`,
    interaction.isManagedLocalSession
      ? "session-workspace-route--managed"
      : "session-workspace-route--unmanaged",
  ].join(" ");

  const launchPendingBanner = (() => {
    const state = session.launch_state;
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

  return (
    <div
      className={workspaceClassName}
      data-control-path={interaction.isManagedLocalSession ? "managed" : "unmanaged"}
      data-runtime-tone={runtime.tone}
    >
      {launchPendingBanner}
      <WorkspaceShell
        sidebar={
          <SessionContextPane
            session={displaySession}
            title={title}
            headThreadSession={headThreadSession}
            threadSessions={threadSessions}
            isViewingHead={isViewingHead}
            onOpenSession={navigateToSession}
            onOpenLatest={() =>
              headThreadSession && navigateToSession(headThreadSession.id)
            }
            continuationNotice={interaction.notice}
            loopModePending={loopModePending}
            onLoopModeChange={handleLoopModeChange}
          />
        }
        main={
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
            sessionEnded={Boolean(session && isSessionClosed(session))}
            headerLeft={
              <div className="session-workspace-header__left">
                <Button variant="ghost" size="sm" onClick={handleBack}>
                  &larr;
                </Button>
                <div className="session-workspace-header__title-stack">
                  <span className="session-workspace-header__name">
                    {title}
                  </span>
                </div>
              </div>
            }
            headerRight={
              confirmingArchive ? (
                <div className="session-detail-archive-confirm">
                  <span className="session-detail-archive-confirm-label">
                    Archive this session?
                  </span>
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
                  Archive
                </Button>
              )
            }
            listRef={registerTimelineList}
            dock={
              <div
                className="session-control-dock"
                data-testid="session-control-dock"
              >
                <SessionRuntimeStrip
                  session={displaySession}
                  interaction={interaction}
                  hostLabel={runtimeHostLabel}
                  elapsedLabel={runtimeElapsedLabel}
                  detailOverride={
                    interaction.isManagedLocalSession ? activeToolDetail : null
                  }
                  variant="dock"
                  testId="session-control-strip"
                />
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
                  composerDisabledReason={interaction.composerDisabledReason}
                  managedLaunchSuggestion={null}
                  submitLabel={interaction.submitLabel}
                  canQueueNextInput={Boolean(
                    displaySession.capabilities?.can_queue_next_input,
                  )}
                  canSteerActiveTurn={Boolean(
                    displaySession.capabilities?.can_steer_active_turn,
                  )}
                  isStalled={Boolean(!displaySession.runtime_facts && displaySession.runtime_display?.is_stalled)}
                  onSessionChanged={(nextSessionId) => {
                    if (!nextSessionId || nextSessionId === session.id) return;
                    navigate(`/timeline/${nextSessionId}`, {
                      replace: true,
                      state: { from: returnTo },
                    });
                  }}
                />
              </div>
            }
          />
        }
      />
    </div>
  );
}

export default function SessionDetailPage() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const location = useLocation();
  const [searchParams] = useSearchParams();

  const highlightEventId = useMemo(() => {
    const raw = searchParams.get("event_id");
    if (!raw) return null;
    const parsed = Number(raw);
    return Number.isFinite(parsed) ? parsed : null;
  }, [searchParams]);

  const shouldAutoResume = searchParams.get("resume") === "1";
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
    />
  );
}
