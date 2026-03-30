/**
 * SessionDetailPage - IDE-style session workspace for one synced transcript.
 *
 * Layout:
 * - Left: session context and continuation lineage
 * - Center: event timeline transcript
 * - Right: inspector for the selected event
 * - Bottom dock: inline live-session / cloud continuation composer
 */

import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "react-hot-toast";
import { Navigate, useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { Button, EmptyState, Spinner } from "../components/ui";
import { SessionChat, type SessionChatTarget } from "../components/SessionChat";
import { EventInspectorPane } from "../components/session-workspace/EventInspectorPane";
import { SessionContextPane } from "../components/session-workspace/SessionContextPane";
import { TimelinePane } from "../components/session-workspace/TimelinePane";
import { WorkspaceShell } from "../components/workspace/WorkspaceShell";
import { useDocumentVisible } from "../hooks/useDocumentVisible";
import { useSessionWorkspace } from "../hooks/useSessionWorkspace";
import { useReadinessFlag } from "../lib/readiness-contract";
import { setSessionLoopMode, type SessionLoopMode } from "../services/api/agents";
import type { AgentSession, AgentSessionThreadResponse, AgentSessionWorkspaceResponse } from "../services/api";
import {
  fetchSessionTurnTelemetry,
  type SessionTurnReview,
} from "../services/api/oikos";
import {
  getSessionInteractionCapabilities,
} from "../lib/sessionWorkspace";
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
  const queryClient = useQueryClient();
  const workspace = useSessionWorkspace(sessionId, { highlightEventId });
  const documentVisible = useDocumentVisible();
  const [loopModeOverride, setLoopModeOverride] = useState<SessionLoopMode | null>(null);
  const [loopModePending, setLoopModePending] = useState(false);

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
    fetchNextPage,
    hasNextPage,
    isFetchingNextPage,
    abandonedEvents,
    selectedKey,
    selectedSelection,
    selectKey,
    registerTimelineList,
  } = workspace;

  const navigateToSession = (nextSessionId: string) => {
    navigate(`/timeline/${nextSessionId}`, {
      replace: nextSessionId === session?.id,
      state: { from: returnTo },
    });
  };

  const handleBack = () => {
    navigate(returnTo);
  };

  const workspaceReady = !sessionLoading && !eventsLoading;

  useReadinessFlag({
    ready: workspaceReady,
    screenshotReady: workspaceReady,
  });

  const turnTelemetryQuery = useQuery({
    queryKey: ["session-turn-telemetry", session?.id],
    queryFn: () => fetchSessionTurnTelemetry(session?.id as string),
    enabled: Boolean(session?.id) && workspaceReady && documentVisible,
    retry: false,
    refetchOnWindowFocus: false,
    staleTime: 60_000,
  });

  const latestTurnReview: SessionTurnReview | null = turnTelemetryQuery.data?.latestReview ?? null;
  const turnReviewLoading = Boolean(session?.id) && turnTelemetryQuery.isLoading;
  const turnReviewUnavailable = Boolean(session?.id) && turnTelemetryQuery.isError;

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
  const effectiveLoopMode = loopModeOverride ?? session.loop_mode;
  const displaySession =
    effectiveLoopMode === session.loop_mode ? session : { ...session, loop_mode: effectiveLoopMode };

  const continuationSourceSession = currentThreadSession || session;
  const interaction = getSessionInteractionCapabilities({
    session: continuationSourceSession,
    isViewingHead,
    headThreadSession,
  });

  const continuationHint = undefined;

  const sessionChatTarget: SessionChatTarget | null =
    interaction.canChatFromBrowser
    ? {
        id: continuationSourceSession.id,
        project: continuationSourceSession.project,
        provider: continuationSourceSession.provider,
      }
    : null;

  const inspectorSelection =
    selectedSelection && selectedSelection.kind !== "message" ? selectedSelection : null;

  const continuationNotice = interaction.notice;

  const handleLoopModeChange = async (nextMode: SessionLoopMode) => {
    if (loopModePending || nextMode === effectiveLoopMode) {
      return;
    }
    setLoopModeOverride(nextMode);
    setLoopModePending(true);
    try {
      const updatedSession = await setSessionLoopMode(session.id, nextMode);
      queryClient.setQueryData<AgentSession>(["agent-session", session.id], (current) =>
        current ? { ...current, loop_mode: updatedSession.loop_mode } : current,
      );
      queryClient.setQueryData<AgentSessionThreadResponse>(["agent-session-thread", session.id], (current) => {
        if (!current) {
          return current;
        }
        return {
          ...current,
          sessions: current.sessions.map((threadSession) =>
            threadSession.id === session.id
              ? { ...threadSession, loop_mode: updatedSession.loop_mode }
              : threadSession,
          ),
        };
      });
      queryClient.setQueriesData<AgentSessionWorkspaceResponse>(
        { queryKey: ["agent-session-workspace", session.id] },
        (current) => {
          if (!current) {
            return current;
          }

          return {
            ...current,
            session:
              current.session.id === session.id
                ? { ...current.session, loop_mode: updatedSession.loop_mode }
                : current.session,
            thread: {
              ...current.thread,
              sessions: current.thread.sessions.map((threadSession) =>
                threadSession.id === session.id
                  ? { ...threadSession, loop_mode: updatedSession.loop_mode }
                  : threadSession,
              ),
            },
          };
        },
      );
      setLoopModeOverride(null);
      toast.success(`Loop mode set to ${nextMode}.`);
    } catch (error) {
      setLoopModeOverride(null);
      toast.error(error instanceof Error ? error.message : "Failed to update loop mode.");
    } finally {
      setLoopModePending(false);
    }
  };

  return (
    <div className="session-workspace-route">
      <WorkspaceShell
        header={
          <div className="session-workspace-header">
            <div className="session-workspace-header__left">
              <Button variant="ghost" size="sm" onClick={handleBack}>
                &larr; Timeline
              </Button>
              <div className="session-workspace-header__context">
                <span className="session-workspace-header__name">{title}</span>
                <span className="session-workspace-header__meta">
                  {threadSessions.length > 1
                    ? `${threadSessions.length} continuations`
                    : "Single continuation"}
                </span>
              </div>
            </div>
          </div>
        }
        sidebar={
          <SessionContextPane
            session={displaySession}
            title={title}
            headThreadSession={headThreadSession}
            threadSessions={threadSessions}
            isViewingHead={isViewingHead}
            onOpenSession={navigateToSession}
            onOpenLatest={() => headThreadSession && navigateToSession(headThreadSession.id)}
            continuationNotice={continuationNotice}
            loopModePending={loopModePending}
            onLoopModeChange={handleLoopModeChange}
            latestTurnReview={latestTurnReview}
            turnReviewLoading={turnReviewLoading}
            turnReviewUnavailable={turnReviewUnavailable}
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
            hasNextPage={hasNextPage ?? false}
            isFetchingNextPage={isFetchingNextPage}
            onFetchNextPage={() => void fetchNextPage()}
            loading={eventsLoading}
            error={eventsError}
            selectedKey={selectedKey}
            onSelectKey={selectKey}
            listRef={registerTimelineList}
            dock={
              sessionChatTarget ? (
                <SessionChat
                  key={`${sessionChatTarget.id}:${interaction.mode}`}
                  session={sessionChatTarget}
                  layout="dock"
                  dockHeaderStyle={interaction.mode === "head" ? "hidden" : "divider"}
                  introEyebrow={
                    interaction.mode === "managed_local"
                      ? "Live session"
                      : interaction.mode === "branch"
                        ? "Cloud branch"
                        : "Cloud continuation"
                  }
                  introTitle={interaction.title}
                  introDescription={interaction.description}
                  hintText={continuationHint}
                  composerPlaceholder={interaction.placeholder}
                  submitLabel={interaction.submitLabel}
                  requireClickForFirstSend={
                    interaction.mode === "branch" || interaction.mode === "promote"
                  }
                  keyboardHintText={interaction.keyboardHint}
                  onSessionChanged={(nextSessionId) => {
                    if (!nextSessionId || nextSessionId === session.id) return;
                    navigate(`/timeline/${nextSessionId}`, {
                      replace: true,
                      state: { from: returnTo },
                    });
                  }}
                />
              ) : null
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
  const returnTo = (location.state as { from?: string } | null)?.from ?? "/timeline";

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
