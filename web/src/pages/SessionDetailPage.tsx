/**
 * SessionDetailPage - IDE-style session workspace for one synced transcript.
 *
 * Layout:
 * - Left: session context and branch lineage
 * - Center: event timeline transcript
 * - Right: inspector for the selected event
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
import { EventInspectorPane } from "../components/session-workspace/EventInspectorPane";
import { SessionContextPane } from "../components/session-workspace/SessionContextPane";
import { SessionRuntimeStrip } from "../components/session-workspace/SessionRuntimeStrip";
import { TimelinePane } from "../components/session-workspace/TimelinePane";
import { WorkspaceShell } from "../components/workspace/WorkspaceShell";
import { useDocumentVisible } from "../hooks/useDocumentVisible";
import { useLoopModeChange } from "../hooks/useLoopModeChange";
import { useSessionWorkspace } from "../hooks/useSessionWorkspace";
import { config } from "../lib/config";
import { useReadinessFlag } from "../lib/readiness-contract";
import { setSessionAction } from "../services/api/agents";
import { DEMO_READ_ONLY_MESSAGE } from "../services/api/base";
import { getSessionInteractionCapabilities } from "../lib/sessionWorkspace";
import "../styles/session-workspace.css";

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
  const documentVisible = useDocumentVisible();

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
    fetchPreviousPage,
    hasPreviousPage,
    isFetchingPreviousPage,
    abandonedEvents,
    selectedKey,
    selectedSelection,
    selectKey,
    handleVisibleSelectionChange,
    registerTimelineList,
  } = workspace;

  const navigateToSession = (nextSessionId: string) => {
    navigate(`/timeline/${nextSessionId}`, {
      replace: nextSessionId === session?.id,
      state: { from: returnTo },
    });
  };

  const handleBack = useCallback(() => {
    navigate(returnTo);
  }, [navigate, returnTo]);
  const handleOpenBranchDock = useCallback(() => {
    const panel = document.querySelector(
      '[data-testid="session-continuation-panel"]',
    );
    if (!(panel instanceof HTMLElement)) return;
    panel.scrollIntoView({ behavior: "smooth", block: "end" });
    const textarea = panel.querySelector("textarea");
    if (textarea instanceof HTMLTextAreaElement && !textarea.disabled) {
      textarea.focus({ preventScroll: true });
    }
  }, []);

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

  const inspectorSelection =
    selectedSelection && selectedSelection.kind !== "message"
      ? selectedSelection
      : null;

  return (
    <div className="session-workspace-route">
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
            onPrimaryAction={handleOpenBranchDock}
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
            headerLeft={
              <div className="session-workspace-header__left">
                <Button variant="ghost" size="sm" onClick={handleBack}>
                  &larr;
                </Button>
                <div className="session-workspace-header__title-stack">
                  <span className="session-workspace-header__name">
                    {title}
                  </span>
                  <SessionRuntimeStrip
                    session={displaySession}
                    interaction={interaction}
                    hostLabel={runtimeHostLabel}
                    variant="inline"
                    testId="session-detail-header-runtime"
                  />
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
        inspector={
          inspectorSelection ? (
            <EventInspectorPane
              selection={inspectorSelection}
              onSelectKey={selectKey}
            />
          ) : undefined
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
