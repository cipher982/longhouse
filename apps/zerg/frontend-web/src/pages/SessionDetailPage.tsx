/**
 * SessionDetailPage - IDE-style session workspace for one synced transcript.
 *
 * Layout:
 * - Left: session context and continuation lineage
 * - Center: event timeline transcript
 * - Right: inspector for the selected event
 * - Bottom dock: inline cloud continuation composer for supported providers
 */

import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { toast } from "react-hot-toast";
import { useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { Button, EmptyState, Spinner } from "../components/ui";
import { SessionChat } from "../components/SessionChat";
import { EventInspectorPane } from "../components/session-workspace/EventInspectorPane";
import { SessionContextPane } from "../components/session-workspace/SessionContextPane";
import { TimelinePane } from "../components/session-workspace/TimelinePane";
import { WorkspaceShell } from "../components/workspace/WorkspaceShell";
import type { ActiveSession } from "../hooks/useActiveSessions";
import { useSessionWorkspace } from "../hooks/useSessionWorkspace";
import { useReadinessFlag } from "../lib/readiness-contract";
import { setSessionLoopMode, type SessionLoopMode } from "../services/api/agents";
import {
  fetchSessionTurnTelemetry,
  type SessionTurnReview,
} from "../services/api/oikos";
import {
  formatProviderLabel,
  getSessionOriginLabel,
  supportsCloudContinuation,
} from "../lib/sessionWorkspace";
import "../styles/session-workspace.css";

export default function SessionDetailPage() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  const [searchParams] = useSearchParams();

  const highlightEventId = useMemo(() => {
    const raw = searchParams.get("event_id");
    if (!raw) return null;
    const parsed = Number(raw);
    return Number.isFinite(parsed) ? parsed : null;
  }, [searchParams]);

  const shouldAutoResume = useMemo(() => searchParams.get("resume") === "1", [searchParams]);

  const workspace = useSessionWorkspace(sessionId || null, { highlightEventId });
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
    totalEntries,
    loadedEntryCount,
    items,
    filteredItems,
    eventsLoading,
    eventsError,
    fetchNextPage,
    hasNextPage,
    isFetchingNextPage,
    eventFilter,
    setEventFilter,
    searchQuery,
    setSearchQuery,
    debouncedSearch,
    messageCount,
    toolRowCount,
    outsideActiveCount,
    abandonedEvents,
    showAbandonedBranches,
    setShowAbandonedBranches,
    selectedKey,
    selectedSelection,
    selectKey,
    registerTimelineList,
  } = workspace;

  const returnTo = (location.state as { from?: string } | null)?.from;

  const navigateToSession = (nextSessionId: string) => {
    navigate(`/timeline/${nextSessionId}`, {
      replace: nextSessionId === session?.id,
      state: { from: returnTo ?? "/timeline" },
    });
  };

  const handleBack = () => {
    navigate(returnTo ?? "/timeline");
  };

  useEffect(() => {
    if (!shouldAutoResume) return;

    const next = new URLSearchParams(searchParams);
    next.delete("resume");
    navigate(
      {
        pathname: location.pathname,
        search: next.toString() ? `?${next.toString()}` : "",
      },
      { replace: true, state: { from: returnTo ?? "/timeline" } },
    );
  }, [shouldAutoResume, searchParams, navigate, location.pathname, returnTo]);

  useReadinessFlag({
    ready: !sessionLoading && !eventsLoading,
    screenshotReady: !sessionLoading && !eventsLoading,
  });

  useEffect(() => {
    setLoopModeOverride(null);
    setLoopModePending(false);
  }, [session?.id, session?.loop_mode]);

  const turnTelemetryQuery = useQuery({
    queryKey: ["session-turn-telemetry", session?.id],
    queryFn: () => fetchSessionTurnTelemetry(session?.id as string),
    enabled: Boolean(session?.id),
    retry: false,
    refetchOnWindowFocus: false,
    staleTime: 30_000,
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
  const providerLabel = formatProviderLabel(continuationSourceSession.provider);
  const canContinueInCloud = supportsCloudContinuation(continuationSourceSession.provider);
  const headOriginLabel = headThreadSession ? getSessionOriginLabel(headThreadSession) : null;
  const sourceOriginLabel = continuationSourceSession
    ? getSessionOriginLabel(continuationSourceSession)
    : null;

  const continuationMode: "unsupported" | "head" | "promote" | "branch" = !canContinueInCloud
    ? "unsupported"
    : !isViewingHead
      ? "branch"
      : continuationSourceSession.continuation_kind === "cloud"
        ? "head"
        : "promote";

  const continuationSubmitLabel =
    continuationMode === "branch"
      ? "Branch in Cloud"
      : continuationMode === "promote"
        ? "Start in Cloud"
        : "Reply";

  const continuationTitle =
    continuationMode === "head"
      ? "Cloud continuation began here"
      : continuationMode === "promote"
        ? "Cloud continuation starts here"
        : continuationMode === "branch"
          ? "New cloud branch starts here"
          : `This ${providerLabel} transcript is synced, but not resumable from the web yet`;

  const continuationDescription =
    continuationMode === "head"
      ? `Earlier turns were synced from ${sourceOriginLabel}. New messages below keep extending this cloud session.`
      : continuationMode === "promote"
        ? `Earlier turns were synced from ${sourceOriginLabel}. Your first message below starts the cloud continuation.`
        : continuationMode === "branch"
          ? `Earlier turns were synced from ${sourceOriginLabel}. Your first message below starts a new cloud branch from this point${headOriginLabel ? ` and leaves the latest ${headOriginLabel} head untouched` : ""}.`
          : `Direct cloud continuation is currently wired for Claude sessions only. This ${providerLabel} transcript is still searchable and auditable here while we close that provider gap.`;

  const continuationHint = undefined;

  const continuationPlaceholder =
    continuationMode === "branch"
      ? "Branch from this point in cloud..."
      : continuationMode === "promote"
        ? "Continue this thread in the cloud..."
        : "Type a message...";

  const continuationKeyboardHint =
    continuationMode === "branch"
      ? 'Press the "Branch in Cloud" button to confirm the new branch.'
      : continuationMode === "promote"
        ? 'Press the "Start in Cloud" button to confirm the first cloud message.'
        : undefined;

  const activeSessionForChat: ActiveSession | null = canContinueInCloud
    ? {
        id: continuationSourceSession.id,
        project: continuationSourceSession.project,
        provider: continuationSourceSession.provider,
        cwd: continuationSourceSession.cwd,
        git_repo: continuationSourceSession.git_repo,
        git_branch: continuationSourceSession.git_branch,
        started_at: continuationSourceSession.started_at,
        ended_at: continuationSourceSession.ended_at,
        last_activity_at:
          continuationSourceSession.ended_at || continuationSourceSession.started_at,
        status: continuationSourceSession.ended_at ? "completed" : "working",
        attention: "auto",
        duration_minutes: 0,
        last_user_message: null,
        last_assistant_message: null,
        message_count:
          continuationSourceSession.user_messages +
          continuationSourceSession.assistant_messages,
        tool_calls: continuationSourceSession.tool_calls,
        presence_state: null,
        presence_tool: null,
        presence_updated_at: null,
        user_state: "active",
        loop_mode:
          continuationSourceSession.id === session.id
            ? effectiveLoopMode
            : continuationSourceSession.loop_mode,
      }
    : null;

  const inspectorSelection =
    selectedSelection && selectedSelection.kind !== "message" ? selectedSelection : null;

  const continuationNotice = !canContinueInCloud
    ? {
        title: `Web continuation unavailable for ${providerLabel}`,
        body: `This ${providerLabel} transcript is still fully searchable here, but direct cloud continuation is currently wired for Claude sessions only.`,
      }
    : null;

  const handleLoopModeChange = async (nextMode: SessionLoopMode) => {
    if (loopModePending || nextMode === effectiveLoopMode) {
      return;
    }
    setLoopModeOverride(nextMode);
    setLoopModePending(true);
    try {
      await setSessionLoopMode(session.id, nextMode);
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
            filteredItems={filteredItems}
            totalEntries={totalEntries}
            loadedEntries={loadedEntryCount}
            eventFilter={eventFilter}
            onEventFilterChange={setEventFilter}
            searchQuery={searchQuery}
            onSearchQueryChange={setSearchQuery}
            debouncedSearch={debouncedSearch}
            messageCount={messageCount}
            toolRowCount={toolRowCount}
            outsideActiveCount={outsideActiveCount}
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
              canContinueInCloud && activeSessionForChat ? (
                <SessionChat
                  key={`${activeSessionForChat.id}:${continuationMode}`}
                  session={activeSessionForChat}
                  layout="dock"
                  dockHeaderStyle={continuationMode === "head" ? "hidden" : "divider"}
                  introEyebrow={
                    continuationMode === "branch" ? "Cloud branch" : "Cloud continuation"
                  }
                  introTitle={continuationTitle}
                  introDescription={continuationDescription}
                  hintText={continuationHint}
                  composerPlaceholder={continuationPlaceholder}
                  submitLabel={continuationSubmitLabel}
                  requireClickForFirstSend={continuationMode !== "head"}
                  keyboardHintText={continuationKeyboardHint}
                  onSessionChanged={(nextSessionId) => {
                    if (!nextSessionId || nextSessionId === session.id) return;
                    navigate(`/timeline/${nextSessionId}`, {
                      replace: true,
                      state: { from: returnTo ?? "/timeline" },
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
