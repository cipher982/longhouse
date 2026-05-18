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
  getSessionFallbackSummary,
  getSessionTitle,
  renderHighlightedText,
  getTurnsColor,
} from "../../lib/sessionUtils";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const SESSION_CARD_HOVER_PREFETCH_DELAY_MS = 180;
const TRANSCRIPT_PREVIEW_CHAR_LIMIT = 180;

export type SessionCardCopyMode = "ai" | "fallback";

function compactCopy(value: string): string {
  return value.trim().replace(/\s+/g, " ");
}

type TimelineStatusLike = {
  label: string;
  seen_at?: string | null;
  seen_at_prefix: string;
};

function formatTimelineStatusMeta(status: TimelineStatusLike | null, relativeNowMs: number): string | null {
  if (!status?.seen_at) {
    return null;
  }
  const prefix = status.seen_at_prefix.trim();
  return `${prefix} ${formatRelativeTime(status.seen_at, relativeNowMs)}`;
}

function runtimeFreshness(status: TimelineStatusLike | null, relativeNowMs: number): "fresh" | "warm" | "stale" | "old" | "unknown" {
  if (!status?.seen_at) {
    return "unknown";
  }
  const seenAtMs = Date.parse(status.seen_at);
  if (!Number.isFinite(seenAtMs)) {
    return "unknown";
  }
  const ageMs = Math.max(0, relativeNowMs - seenAtMs);
  if (ageMs <= 5 * 60 * 1000) {
    return "fresh";
  }
  if (ageMs <= 60 * 60 * 1000) {
    return "warm";
  }
  if (ageMs <= 6 * 60 * 60 * 1000) {
    return "stale";
  }
  return "old";
}

function isAnimatedRuntimeTone(tone: string): boolean {
  return tone === "thinking" || tone === "running";
}

type TranscriptPreviewCard = {
  text: string;
  fullText: string;
  label: "Live output" | "Latest output";
};

function getTranscriptPreviewCard(session: TimelineSessionCard["head"]): TranscriptPreviewCard | null {
  const preview = session.transcript_preview;
  if (!preview || preview.is_stale) {
    return null;
  }
  const text = preview.text?.trim();
  if (!text) {
    return null;
  }
  const compact = text.replace(/\s+/g, " ");
  const previewText =
    compact.length <= TRANSCRIPT_PREVIEW_CHAR_LIMIT
      ? compact
      : `${compact.slice(0, TRANSCRIPT_PREVIEW_CHAR_LIMIT - 1).trimEnd()}...`;
  return {
    text: previewText,
    fullText: text,
    label: preview.is_complete ? "Latest output" : "Live output",
  };
}

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
  copyMode?: SessionCardCopyMode;
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
  copyMode = "ai",
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
  const timelineCardStatus = timelineCard?.status ?? null;
  const rawCardStatus = timelineCardStatus ?? runtime.factStatus ?? null;
  const processState = runtime.runtimeFacts?.process_state ?? null;
  const phaseKind = runtime.runtimeFacts?.phase?.kind?.trim() || null;
  const hasProcessAxis = processState === "running" || processState === "closed" || processState === "unknown";
  const cardStatusIsProcessOnly =
    hasProcessAxis &&
    phaseKind == null &&
    rawCardStatus != null &&
    (rawCardStatus.tone === "inactive" || rawCardStatus.tone === "closed");
  const cardStatus = cardStatusIsProcessOnly ? null : rawCardStatus;
  const runtimeMetaLabel =
    formatTimelineStatusMeta(cardStatus === timelineCardStatus ? timelineCardStatus : null, relativeNowMs) ??
    getRuntimeMetaLabel(runtime, relativeNowMs);
  const fallbackOwnershipLabel = interaction.isManagedLocalSession ? "Managed" : "Unmanaged";
  const ownershipLabel = timelineCard?.ownership.label || resolveSessionOwnershipLabel(runtime, fallbackOwnershipLabel);
  const fallbackControlPath: "managed" | "unmanaged" = ownershipLabel === "Managed" ? "managed" : "unmanaged";
  const factControlPath =
    runtime.runtimeFacts?.control_path === "managed"
      ? "managed"
      : runtime.runtimeFacts?.control_path === "unmanaged"
        ? "unmanaged"
        : null;
  const displayControlPath =
    runtime.runtimeDisplay?.control_path === "managed"
      ? "managed"
      : runtime.runtimeDisplay?.control_path === "unmanaged"
        ? "unmanaged"
        : null;
  const controlPath: "managed" | "unmanaged" =
    factControlPath != null
      ? factControlPath
      : displayControlPath != null
        ? displayControlPath
        : fallbackControlPath;
  const ownershipTone = controlPath === "managed" ? "managed" : "unmanaged";
  const runtimePhaseLabel = cardStatus?.label || resolveSessionStatusLabel(runtime, controlPath);
  const cardRuntimeTone = cardStatus?.tone ?? timelineCard?.border_tone ?? runtime.tone;
  const runtimeFreshnessTone = runtimeFreshness(timelineCardStatus, relativeNowMs);
  const processPillLabel =
    processState === "running"
      ? "Process running"
      : processState === "unknown"
        ? "Process unknown"
        : processState === "closed"
          ? "Process closed"
          : null;
  const processPillTitle =
    processState === "running"
      ? "The provider process is still running on the host."
      : processState === "unknown"
        ? "Longhouse has not verified whether the provider process is still running."
        : processState === "closed"
          ? "The provider process is closed."
          : undefined;
  const projectLabel = getProjectLabel(session);
  const aiCopyEnabled = copyMode === "ai";
  const title = getSessionTitle(session, { preferAi: aiCopyEnabled });
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
  const transcriptPreview = getTranscriptPreviewCard(session);
  const cardActionLabel = groupedQueryMode ? "Open match" : "Open session";
  const hasControlPath = interaction.liveControlAvailable || interaction.hostReattachAvailable;
  // Lifecycle is the closure axis. The reducer only closes on explicit
  // terminal or process-gone facts.
  const lifecycle = runtime.runtimeFacts?.lifecycle?.state ?? runtime.runtimeDisplay?.lifecycle ?? null;
  const hasCurrentControlledPresence = hasControlPath && runtime.presenceState != null;
  const isClosedSession =
    rawCardStatus?.tone === "closed" ||
    rawCardStatus?.label === "Closed" ||
    (lifecycle != null
      ? lifecycle === "closed"
      : isSessionClosed(session) && !hasCurrentControlledPresence);
  const showTranscriptPreview =
    aiCopyEnabled &&
    !showKeywordSnippet &&
    !showSemanticSnippet &&
    !isClosedSession &&
    transcriptPreview != null;
  const showSummary =
    aiCopyEnabled && !showTranscriptPreview && !showKeywordSnippet && !showSemanticSnippet && !!session.summary;
  const fallbackSummary = getSessionFallbackSummary(session, TRANSCRIPT_PREVIEW_CHAR_LIMIT);
  const fallbackDuplicatesTitle = compactCopy(fallbackSummary) === compactCopy(title);
  const showFallbackSummary =
    !showSummary &&
    !showTranscriptPreview &&
    !showKeywordSnippet &&
    !showSemanticSnippet &&
    !fallbackDuplicatesTitle &&
    fallbackSummary.length > 0;
  const showProcessPill =
    !isClosedSession && processPillLabel != null && (controlPath === "unmanaged" || processState === "running");
  // Always render the runtime pill for unmanaged sessions so unknown or
  // transcript-only state is visible instead of hidden.
  const hasRuntimeAxes =
    runtime.runtimeFacts?.control_path === "managed" ||
    runtime.runtimeFacts?.control_path === "unmanaged" ||
    runtime.runtimeDisplay?.control_path === "managed" ||
    runtime.runtimeDisplay?.control_path === "unmanaged";
  const showRuntimePill = !isClosedSession && (cardStatus != null || (!hasProcessAxis && (runtime.hasSignal || hasRuntimeAxes)));
  // Outcome labels are semantic summaries; keep their chips neutral across runtime sources.
  const runtimePillTone = cardStatus?.tone || (runtimePhaseLabel === "Active" ? "active" : runtime.tone);
  const useToneRuntimeDot = cardStatus != null || runtimePhaseLabel === "Active";
  const animateRuntimeDot = controlPath === "managed" && isAnimatedRuntimeTone(runtimePillTone);
  const closedSessionTitle = "This process is closed.";
  const cardClassName = [
    "session-card",
    `session-card--${ownershipTone}`,
    `session-card--runtime-${runtimeFreshnessTone}`,
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
      data-runtime-freshness={runtimeFreshnessTone}
      data-control-path={controlPath}
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
              className={`session-card-ownership-pill session-card-ownership-pill--${ownershipTone}`}
              data-testid="session-card-ownership"
              title={
                ownershipLabel === "Managed"
                  ? "Longhouse owns the control path for this session."
                  : "Longhouse imported or discovered this session without a control path."
              }
            >
              {ownershipLabel}
            </span>
            {showProcessPill ? (
              <span
                className={`session-card-process-pill session-card-process-pill--${processState}`}
                data-testid="session-card-process-state"
                title={processPillTitle}
              >
                {processPillLabel}
              </span>
            ) : null}
            {isClosedSession ? (
              <span
                className="session-card-closed-pill"
                data-testid="session-card-closed-state"
                title={closedSessionTitle}
              >
                Closed
              </span>
            ) : null}
            {showRuntimePill && (
              <div
                className={`session-card-runtime session-card-runtime--${runtimePillTone}`}
                data-testid="session-card-runtime"
              >
                {useToneRuntimeDot ? (
                  <span
                    className={`session-card-runtime-dot${animateRuntimeDot ? " session-card-runtime-dot--animated" : ""}`}
                    aria-hidden="true"
                  />
                ) : (
                  <PresenceBadge
                    state={cardStatus ? null : runtime.presenceState}
                    tool={cardStatus ? null : runtime.presenceTool}
                    compact
                    animateCompact={animateRuntimeDot}
                    showUnknown={cardStatus != null || runtime.truthTier === "stale"}
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
          {showTranscriptPreview && (
            <div
              className="session-card-transcript-preview"
              data-testid="session-card-transcript-preview"
              title={transcriptPreview?.fullText}
            >
              <span className="session-card-transcript-preview__label">
                {transcriptPreview?.label}
              </span>
              <span className="session-card-transcript-preview__text">
                {transcriptPreview?.text}
              </span>
            </div>
          )}
          {showFallbackSummary && (
            <div className="session-card-summary">
              {fallbackSummary}
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
