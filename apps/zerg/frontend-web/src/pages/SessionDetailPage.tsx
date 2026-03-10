/**
 * SessionDetailPage - IDE-style session workspace for one synced transcript.
 *
 * Layout:
 * - Left: session context and continuation lineage
 * - Center: event timeline navigator
 * - Right: inspector for the selected event
 * - Bottom: continuation dock
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { Button, EmptyState, Spinner } from "../components/ui";
import { SessionChat } from "../components/SessionChat";
import { EventInspectorPane } from "../components/session-workspace/EventInspectorPane";
import { SessionContextPane } from "../components/session-workspace/SessionContextPane";
import { TimelinePane } from "../components/session-workspace/TimelinePane";
import { WorkspaceShell } from "../components/workspace/WorkspaceShell";
import type { ActiveSession } from "../hooks/useActiveSessions";
import { useSessionWorkspace } from "../hooks/useSessionWorkspace";
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
  const continuationSectionRef = useRef<HTMLDivElement | null>(null);
  const [continuationOpen, setContinuationOpen] = useState(false);

  const highlightEventId = useMemo(() => {
    const raw = searchParams.get("event_id");
    if (!raw) return null;
    const parsed = Number(raw);
    return Number.isFinite(parsed) ? parsed : null;
  }, [searchParams]);

  const shouldAutoResume = useMemo(() => searchParams.get("resume") === "1", [searchParams]);

  const workspace = useSessionWorkspace(sessionId || null, { highlightEventId });

  const {
    session,
    sessionLoading,
    sessionError,
    threadSessions,
    currentThreadSession,
    headThreadSession,
    isViewingHead,
    totalEvents,
    events,
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

  const focusContinuationComposer = (focusComposer: boolean) => {
    if (!focusComposer) return;

    window.setTimeout(() => {
      const textarea = continuationSectionRef.current?.querySelector("textarea");
      if (textarea instanceof HTMLTextAreaElement) {
        textarea.focus();
      }
    }, 0);
  };

  useEffect(() => {
    setContinuationOpen(false);
  }, [sessionId]);

  useEffect(() => {
    if (!continuationOpen) return;

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setContinuationOpen(false);
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [continuationOpen]);

  useEffect(() => {
    if (!shouldAutoResume || !session) return;

    setContinuationOpen(supportsCloudContinuation(session.provider));
    focusContinuationComposer(supportsCloudContinuation(session.provider));

    const next = new URLSearchParams(searchParams);
    next.delete("resume");
    navigate(
      {
        pathname: location.pathname,
        search: next.toString() ? `?${next.toString()}` : "",
      },
      { replace: true, state: { from: returnTo ?? "/timeline" } },
    );
  }, [shouldAutoResume, session, searchParams, navigate, location.pathname, returnTo]);

  useEffect(() => {
    if (!sessionLoading && !eventsLoading) {
      document.body.setAttribute("data-ready", "true");
      document.body.setAttribute("data-screenshot-ready", "true");
    }
    return () => {
      document.body.removeAttribute("data-ready");
      document.body.removeAttribute("data-screenshot-ready");
    };
  }, [sessionLoading, eventsLoading]);

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

  const continuationCtaLabel =
    continuationMode === "branch"
      ? "Branch from Here"
      : canContinueInCloud
        ? "Continue in Cloud"
        : "Latest Context";

  const continuationTitle =
    continuationMode === "head"
      ? "Keep going on the current cloud branch"
      : continuationMode === "promote"
        ? "Continue this thread in cloud"
        : continuationMode === "branch"
          ? "Start a new cloud continuation from this point"
          : `This ${providerLabel} transcript is synced, but not resumable from the web yet`;

  const continuationDescription =
    continuationMode === "head"
      ? "This is the writable head for the thread. Messages you send here keep extending the current cloud continuation."
      : continuationMode === "promote"
        ? `This is the latest synced head from ${sourceOriginLabel}. Your first message here will create the cloud continuation for the thread.`
        : continuationMode === "branch"
          ? `You are viewing an older continuation from ${sourceOriginLabel}. Sending a message here creates a new cloud continuation from this history instead of mutating the latest head${headOriginLabel ? ` (${headOriginLabel})` : ""}.`
          : `Direct cloud continuation is currently wired for Claude sessions only. This ${providerLabel} transcript is still searchable and auditable here while we close that provider gap.`;

  const continuationEmptyTitle =
    continuationMode === "branch"
      ? "Send a message to branch from this history."
      : continuationMode === "promote"
        ? "Send the first cloud message for this thread."
        : "Start a conversation with this session.";

  const continuationHint =
    continuationMode === "branch"
      ? "Longhouse will create a new cloud continuation and leave the current transcript immutable."
      : continuationMode === "promote"
        ? "Longhouse will create the cloud continuation on first send and keep the current transcript as the source branch."
        : "Context from previous turns will be preserved via --resume.";

  const continuationPlaceholder =
    continuationMode === "branch"
      ? "Start a new cloud continuation from this point..."
      : continuationMode === "promote"
        ? "Continue this thread in the cloud..."
        : "Type a message...";

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

  return (
    <div className="session-workspace-route">
      <WorkspaceShell
        header={
          <div className="session-workspace-header">
            <div className="session-workspace-header__left">
              <Button variant="ghost" onClick={handleBack}>
                &larr; Timeline
              </Button>
              <div className="session-workspace-header__titles">
                <div className="session-workspace-header__eyebrow">Session Workspace</div>
                <h1 className="session-workspace-header__title">{title}</h1>
                <div className="session-workspace-header__subtitle">
                  {threadSessions.length > 1
                    ? `${threadSessions.length} continuations`
                    : "Single continuation"}{" "}
                  · {totalEvents} events
                </div>
              </div>
            </div>
            <div className="session-workspace-header__actions">
              {canContinueInCloud ? (
                <Button
                  variant="primary"
                  size="sm"
                  onClick={() => {
                    setContinuationOpen(true);
                    focusContinuationComposer(true);
                  }}
                >
                  {continuationCtaLabel}
                </Button>
              ) : null}
            </div>
          </div>
        }
        sidebar={
          <SessionContextPane
            session={session}
            title={title}
            headThreadSession={headThreadSession}
            threadSessions={threadSessions}
            isViewingHead={isViewingHead}
            onOpenSession={navigateToSession}
            onOpenLatest={() => headThreadSession && navigateToSession(headThreadSession.id)}
            continuationNotice={continuationNotice}
          />
        }
        main={
          <TimelinePane
            items={items}
            filteredItems={filteredItems}
            totalEvents={totalEvents}
            loadedEvents={events.length}
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

      {continuationOpen && canContinueInCloud && activeSessionForChat ? (
        <div
          className="modal-overlay session-workspace-modal-overlay"
          data-testid="session-continuation-panel"
          onClick={() => setContinuationOpen(false)}
        >
          <div
            ref={continuationSectionRef}
            className="modal-container session-workspace-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="session-continuation-title"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="session-workspace-modal__header">
              <div className="session-workspace-modal__copy">
                <div className="session-workspace-modal__eyebrow">Cloud Continuation</div>
                <h2 id="session-continuation-title" className="session-workspace-modal__title">
                  {continuationTitle}
                </h2>
                <p className="session-workspace-modal__description">{continuationDescription}</p>
              </div>
              <Button
                variant="ghost"
                size="sm"
                aria-label="Close continuation"
                onClick={() => setContinuationOpen(false)}
              >
                Close
              </Button>
            </div>

            <div className="session-workspace-modal__chat">
              <SessionChat
                session={activeSessionForChat}
                emptyStateTitle={continuationEmptyTitle}
                hintText={continuationHint}
                composerPlaceholder={continuationPlaceholder}
                onSessionChanged={(nextSessionId) => {
                  if (!nextSessionId || nextSessionId === session.id) return;
                  navigate(`/timeline/${nextSessionId}`, {
                    replace: true,
                    state: { from: returnTo ?? "/timeline" },
                  });
                }}
              />
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
