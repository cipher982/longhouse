/**
 * SessionCard — timeline card for a single session thread.
 */

import { useState, useEffect, useCallback, useRef } from "react";
import { Card } from "../ui";
import { PresenceBadge } from "../PresenceBadge";
import { type TimelineSessionCard, getTimelineCardAnchor } from "../../services/api/agents";
import {
  isSessionClosed,
  resolveSessionOwnershipLabel,
  resolveSessionRuntimeState,
  resolveSessionStatusLabel,
} from "../../lib/sessionRuntime";
import { getSessionInteractionCapabilities } from "../../lib/sessionWorkspace";
import {
  formatRelativeTime,
  getBranchLabel,
  getRuntimeMetaLabel,
  getProjectLabel,
  getSessionTitle,
  renderHighlightedText,
  getTurnsColor,
} from "../../lib/sessionUtils";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const SESSION_CARD_HOVER_PREFETCH_DELAY_MS = 180;

// ---------------------------------------------------------------------------
// SessionCard
// ---------------------------------------------------------------------------

export interface SessionCardProps {
  thread: TimelineSessionCard;
  onClick: () => void;
  onPrefetch?: () => void;
  allowHoverPrefetch?: () => boolean;
  onArchive?: () => void;
  highlightQuery?: string;
  isSemanticResult?: boolean;
  groupedQueryMode?: boolean;
  relativeNowMs: number;
}

export function SessionCard({
  thread,
  onClick,
  onPrefetch,
  allowHoverPrefetch,
  onArchive,
  highlightQuery,
  isSemanticResult,
  groupedQueryMode = false,
  relativeNowMs,
}: SessionCardProps) {
  const [confirming, setConfirming] = useState(false);
  const hoverPrefetchTimerRef = useRef<number | null>(null);
  const detailSession = thread.detail;
  const session = groupedQueryMode ? detailSession : thread.head;
  const interaction = getSessionInteractionCapabilities({ session });
  const turnCount = session.user_messages;
  const toolCount = session.tool_calls;
  const runtime = resolveSessionRuntimeState(session);
  const timelineCard = session.timeline_card ?? null;
  const timelineCardStatus = runtime.factStatus ? null : (timelineCard?.status ?? null);
  const runtimeMetaLabel =
    getRuntimeMetaLabel(runtime, relativeNowMs) ??
    (timelineCardStatus?.seen_at
      ? `Seen ${formatRelativeTime(timelineCardStatus.seen_at, relativeNowMs)}`
      : null);
  const fallbackOwnershipLabel = interaction.isManagedLocalSession ? "Managed" : "Unmanaged";
  const ownershipLabel = timelineCard?.ownership.label || resolveSessionOwnershipLabel(runtime, fallbackOwnershipLabel);
  const fallbackControlPath = ownershipLabel === "Managed" ? "managed" : "unmanaged";
  const runtimePhaseLabel = runtime.factStatus?.label || timelineCardStatus?.label || resolveSessionStatusLabel(runtime, fallbackControlPath);
  const cardRuntimeTone = runtime.factStatus?.tone ?? timelineCard?.border_tone ?? runtime.tone;

  const projectLabel = getProjectLabel(session);
  const title = getSessionTitle(session);
  const branchLabel = getBranchLabel(session.git_branch);
  const cardRuntimeMetaParts = [runtimeMetaLabel].filter(Boolean);
  const showContinuationCount = !groupedQueryMode && thread.continuation_count > 1;
  const secondaryStatsLabel = groupedQueryMode
    ? `Matched ${formatRelativeTime(detailSession.started_at, relativeNowMs)}`
    : [
        `Started ${formatRelativeTime(thread.root.started_at, relativeNowMs)}`,
        showContinuationCount ? `${thread.continuation_count} continuations` : null,
      ].filter(Boolean).join(" • ");

  const showKeywordSnippet = !isSemanticResult && !!highlightQuery && !!detailSession.match_snippet;
  const showSemanticSnippet = isSemanticResult && !!detailSession.match_snippet;
  const showSummary = !showKeywordSnippet && !showSemanticSnippet && !!session.summary;
  const showGenerating = !showKeywordSnippet && !showSemanticSnippet && !session.summary && !session.summary_title;
  const cardActionLabel = groupedQueryMode ? "Open match" : "Open session";
  const hasControlPath = interaction.liveControlAvailable || interaction.hostReattachAvailable;
  // Lifecycle is the closure axis. The reducer only closes on explicit
  // terminal or process-gone facts.
  const lifecycle = runtime.runtimeFacts?.lifecycle?.state ?? runtime.runtimeDisplay?.lifecycle ?? null;
  const hasCurrentControlledPresence = hasControlPath && runtime.presenceState != null;
  const isClosedSession =
    timelineCardStatus?.tone === "closed" ||
    timelineCardStatus?.label === "Closed" ||
    (lifecycle != null
      ? lifecycle === "closed"
      : isSessionClosed(session) && !hasCurrentControlledPresence);
  // Always render the runtime pill for unmanaged sessions so unknown or
  // transcript-only state is visible instead of hidden.
  const hasRuntimeAxes =
    runtime.runtimeFacts?.control_path === "managed" ||
    runtime.runtimeFacts?.control_path === "unmanaged" ||
    runtime.runtimeDisplay?.control_path === "managed" ||
    runtime.runtimeDisplay?.control_path === "unmanaged";
  const showRuntimePill = !isClosedSession && (runtime.factStatus != null || timelineCardStatus != null || runtime.hasSignal || hasRuntimeAxes);
  // Outcome labels are semantic summaries; keep their chips neutral across runtime sources.
  const runtimePillTone = runtime.factStatus?.tone || timelineCardStatus?.tone || (runtimePhaseLabel === "Active" ? "active" : runtime.tone);
  const cardClassName = [
    "session-card",
    confirming ? "session-card--confirming" : "",
    isClosedSession ? "session-card--closed" : "",
    !isClosedSession && runtime.isExecuting ? "session-card--live" : "",
    !isClosedSession && runtime.isIdle ? "session-card--idle" : "",
    !isClosedSession && cardRuntimeTone === "thinking" ? "session-card--thinking" : "",
    !isClosedSession && cardRuntimeTone === "running" ? "session-card--running" : "",
    !isClosedSession && cardRuntimeTone === "blocked" ? "session-card--blocked" : "",
  ].filter(Boolean).join(" ");

  const clearHoverPrefetchTimer = useCallback(() => {
    if (hoverPrefetchTimerRef.current != null) {
      window.clearTimeout(hoverPrefetchTimerRef.current);
      hoverPrefetchTimerRef.current = null;
    }
  }, []);

  useEffect(() => clearHoverPrefetchTimer, [clearHoverPrefetchTimer]);

  const scheduleHoverPrefetch = useCallback(() => {
    if (!onPrefetch) {
      return;
    }

    clearHoverPrefetchTimer();
    hoverPrefetchTimerRef.current = window.setTimeout(() => {
      hoverPrefetchTimerRef.current = null;
      if (allowHoverPrefetch && !allowHoverPrefetch()) {
        return;
      }
      onPrefetch();
    }, SESSION_CARD_HOVER_PREFETCH_DELAY_MS);
  }, [allowHoverPrefetch, clearHoverPrefetchTimer, onPrefetch]);

  const handleFocusPrefetch = useCallback(() => {
    clearHoverPrefetchTimer();
    onPrefetch?.();
  }, [clearHoverPrefetchTimer, onPrefetch]);

  return (
    <Card
      className={cardClassName}
      onMouseEnter={scheduleHoverPrefetch}
      onMouseLeave={clearHoverPrefetchTimer}
      onFocus={handleFocusPrefetch}
      onBlur={clearHoverPrefetchTimer}
      onPointerDown={(event) => {
        if (event.pointerType === "touch" || event.pointerType === "pen") {
          clearHoverPrefetchTimer();
          onPrefetch?.();
        }
      }}
      data-testid="session-card"
      data-session-id={detailSession.id}
      data-thread-id={thread.thread_id}
      data-runtime-tone={isClosedSession ? "closed" : runtimePillTone}
      data-card-state={isClosedSession ? "closed" : "actionable"}
    >
      {!confirming && (
        <button
          type="button"
          className="session-card-hitbox"
          onClick={onClick}
          aria-label={title ? `${cardActionLabel}: ${title}` : cardActionLabel}
        />
      )}

      <div className={`session-card-content${confirming ? "" : " session-card-content--passthrough"}`}>
        <div className="session-card-header">
          <div className="session-card-project">{projectLabel}</div>
          <div
            className={
              onArchive && !confirming
                ? "session-card-header-right session-card-header-right--with-archive"
                : "session-card-header-right"
            }
          >
            <span className="session-card-time">{formatRelativeTime(getTimelineCardAnchor(thread), relativeNowMs)}</span>
          </div>
        </div>

        <div className="session-card-context">
          <div className="session-card-identity">
            <div className="session-card-identity-primary">
              <span className="session-card-provider-badge">
                <span className="provider-name">{session.provider}</span>
              </span>
              {branchLabel && (
                <span
                  className="session-card-branch-pill"
                  data-testid="session-card-branch"
                  title={`Git branch: ${branchLabel}`}
                >
                  {branchLabel}
                </span>
              )}
            </div>
          </div>

          <div className="session-card-status">
            <span
              className="session-card-ownership-pill session-card-ownership-pill--neutral"
              data-testid="session-card-ownership"
              title={
                ownershipLabel === "Managed"
                  ? "Longhouse owns the control path for this session."
                  : "Longhouse imported or discovered this session without a control path."
              }
            >
              {ownershipLabel}
            </span>
            {isClosedSession ? (
              <span
                className="session-card-closed-pill"
                data-testid="session-card-closed-state"
                title="This process is closed."
              >
                Closed
              </span>
            ) : null}
            {showRuntimePill && (
              <div
                className={`session-card-runtime session-card-runtime--${runtimePillTone}`}
                data-testid="session-card-runtime"
              >
                {runtimePhaseLabel === "Active" ? (
                  <span className="session-card-runtime-dot" aria-hidden="true" />
                ) : (
                  <PresenceBadge
                    state={runtime.factStatus ? null : runtime.presenceState}
                    tool={runtime.factStatus ? null : runtime.presenceTool}
                    compact
                    showUnknown={runtime.factStatus != null || runtime.truthTier === "stale"}
                  />
                )}
                <span className="session-card-runtime-phase">{runtimePhaseLabel}</span>
                {cardRuntimeMetaParts.length > 0 && (
                  <span className="session-card-runtime-meta">
                    {cardRuntimeMetaParts.join(" • ")}
                  </span>
                )}
              </div>
            )}
          </div>
        </div>

        <div className="session-card-body">
          {title && <div className="session-card-title">{title}</div>}
          {showSummary && (
            <div className="session-card-summary">{session.summary}</div>
          )}
          {showGenerating && (
            <div className="session-card-summary session-card-summary--pending">
              Generating summary<span className="session-card-dots" aria-hidden="true" />
            </div>
          )}
          {showKeywordSnippet && (
            <div className="session-card-snippet">
              {renderHighlightedText(detailSession.match_snippet!, highlightQuery!)}
            </div>
          )}
          {showSemanticSnippet && (
            <div className="session-card-snippet session-card-snippet--ai">
              {detailSession.match_snippet}
            </div>
          )}
          {isSemanticResult && detailSession.match_score != null && detailSession.match_score >= 0.5 && (
            <div className="session-card-score" title={`Semantic similarity: ${Math.round(detailSession.match_score * 100)}%`}>
              {Math.round(detailSession.match_score * 100)}% match
            </div>
          )}
        </div>

        {!confirming && (
          <div className="session-card-footer">
            <div className="session-card-stats">
              <div className="session-card-stats-primary">
                <span className="session-stat" style={{ color: getTurnsColor(turnCount) }}>{turnCount} {turnCount === 1 ? 'turn' : 'turns'}</span>
                <span className="session-stat-separator">&middot;</span>
                <span className="session-stat">{toolCount} {toolCount === 1 ? 'tool' : 'tools'}</span>
              </div>
              <div className="session-card-stats-secondary">
                <span className="session-stat session-stat--secondary">
                  {secondaryStatsLabel}
                </span>
              </div>
            </div>
          </div>
        )}
      </div>

      {onArchive && !confirming && (
        <button
          type="button"
          className="session-card-archive-btn"
          onClick={() => setConfirming(true)}
          aria-label="Archive session"
          title="Archive"
        >
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <polyline points="3 6 5 6 21 6" />
            <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
            <path d="M10 11v6M14 11v6" />
            <path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2" />
          </svg>
        </button>
      )}

      {confirming && (
        <div className="session-card-confirm-row">
          <span className="session-card-confirm-label">Archive this session?</span>
          <button
            type="button"
            className="session-card-confirm-cancel"
            onClick={() => setConfirming(false)}
          >
            Cancel
          </button>
          <button
            type="button"
            className="session-card-confirm-ok"
            onClick={() => { setConfirming(false); onArchive?.(); }}
          >
            Archive
          </button>
        </div>
      )}
    </Card>
  );
}
