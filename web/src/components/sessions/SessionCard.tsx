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
  compatibilityMode?: boolean;
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
  compatibilityMode = false,
  relativeNowMs,
}: SessionCardProps) {
  const [confirming, setConfirming] = useState(false);
  const hoverPrefetchTimerRef = useRef<number | null>(null);
  const detailSession = thread.detail;
  const session = compatibilityMode ? detailSession : thread.head;
  const interaction = getSessionInteractionCapabilities({ session });
  const turnCount = session.user_messages;
  const toolCount = session.tool_calls;
  const runtime = resolveSessionRuntimeState(session);
  const runtimeMetaLabel = getRuntimeMetaLabel(runtime);
  const fallbackOwnershipLabel = interaction.isManagedLocalSession ? "Managed" : "Unmanaged";
  const ownershipLabel = resolveSessionOwnershipLabel(runtime, fallbackOwnershipLabel);
  const ownershipTone = ownershipLabel === "Managed" ? "success" : "neutral";
  const fallbackControlPath = ownershipLabel === "Managed" ? "managed" : "unmanaged";
  const runtimePhaseLabel = resolveSessionStatusLabel(runtime, fallbackControlPath);

  const projectLabel = getProjectLabel(session);
  const title = getSessionTitle(session);
  const cardRuntimeMetaParts = [runtimeMetaLabel].filter(Boolean);
  const showContinuationCount = !compatibilityMode && thread.continuation_count > 1;
  const secondaryStatsLabel = compatibilityMode
    ? `Matched ${formatRelativeTime(detailSession.started_at, relativeNowMs)}`
    : [
        `Started ${formatRelativeTime(thread.root.started_at, relativeNowMs)}`,
        showContinuationCount ? `${thread.continuation_count} continuations` : null,
      ].filter(Boolean).join(" • ");

  const showKeywordSnippet = !isSemanticResult && !!highlightQuery && !!detailSession.match_snippet;
  const showSemanticSnippet = isSemanticResult && !!detailSession.match_snippet;
  const showSummary = !showKeywordSnippet && !showSemanticSnippet && !!session.summary;
  const showGenerating = !showKeywordSnippet && !showSemanticSnippet && !session.summary && !session.summary_title;
  const cardActionLabel = compatibilityMode ? "Open match" : "Open session";
  const hasControlPath = interaction.liveControlAvailable || interaction.hostReattachAvailable;
  // Phase 3 of session-liveness-honesty: lifecycle=="closed" is the new
  // ground-truth signal. The backend only emits it when we have an
  // explicit terminal_signal (Phase 6 will also emit it on confirmed
  // process-gone via machine-agent bindings). Legacy fallback: accept
  // explicit terminal_state for older payloads that predate the axis.
  // Phase 3: lifecycle is the closure axis. When the backend tells us it
  // is "closed" that wins unconditionally (the reducer only closes on
  // ground truth). Legacy payloads without the axis fall back via
  // isSessionClosed's terminal_state path, plus a presence guard so an
  // actively-controlled session is not misreported.
  const lifecycle = runtime.runtimeDisplay?.lifecycle ?? null;
  const hasCurrentControlledPresence = hasControlPath && runtime.presenceState != null;
  const isClosedSession =
    lifecycle != null
      ? lifecycle === "closed"
      : isSessionClosed(session) && !hasCurrentControlledPresence;
  // Phase 3: always render the runtime pill for unmanaged sessions so the
  // card states "Stale" / "Unknown" honestly instead of hiding them.
  const hasRuntimeAxes =
    runtime.runtimeDisplay?.control_path === "managed" ||
    runtime.runtimeDisplay?.control_path === "unmanaged";
  const showRuntimePill = !isClosedSession && (runtime.hasSignal || hasRuntimeAxes);
  const showOwnershipPill = true;
  const showStatusRow = showOwnershipPill || isClosedSession || showRuntimePill;
  // Outcome labels are semantic summaries; keep their chips neutral across runtime sources.
  const runtimePillTone =
    runtimePhaseLabel === "Active" || runtimePhaseLabel === "Process seen"
      ? "active"
      : runtime.tone;
  const cardClassName = [
    "session-card",
    confirming ? "session-card--confirming" : "",
    isClosedSession ? "session-card--closed" : "",
    !isClosedSession && runtime.isExecuting ? "session-card--live" : "",
    !isClosedSession && runtime.isIdle ? "session-card--idle" : "",
    !isClosedSession && runtime.tone === "inferred" ? "session-card--inferred" : "",
    !isClosedSession && runtime.tone === "thinking" ? "session-card--thinking" : "",
    !isClosedSession && runtime.tone === "running" ? "session-card--running" : "",
    !isClosedSession && runtime.tone === "needs-user" ? "session-card--needs-user" : "",
    !isClosedSession && runtime.tone === "blocked" ? "session-card--blocked" : "",
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
            </div>
          </div>

          {showStatusRow && (
            <div className="session-card-status">
              {showOwnershipPill ? (
                <span
                  className={`session-card-ownership-pill session-card-ownership-pill--${ownershipTone}`}
                  data-testid="session-card-ownership"
                  title={
                    ownershipLabel === "Managed"
                      ? "Longhouse owns the live control path for this session."
                      : "Longhouse imported or discovered this session without a live control path."
                  }
                >
                  {ownershipLabel}
                </span>
              ) : null}
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
                      state={runtime.presenceState}
                      tool={runtime.presenceTool}
                      compact
                      heuristicActive={runtime.heuristicActive}
                      showUnknown={runtime.truthTier === "stale"}
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
          )}
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
