/**
 * SessionCard — timeline card for a single session thread.
 */

import { useState, useEffect, useCallback, useRef } from "react";
import { Card } from "../ui";
import { PresenceBadge } from "../PresenceBadge";
import { type TimelineSessionCard, getTimelineCardAnchor } from "../../services/api/agents";
import { resolveSessionRuntimeState } from "../../lib/sessionRuntime";
import { getProviderColor, getSessionInteractionCapabilities } from "../../lib/sessionWorkspace";
import { normalizeExecutionVenueLabel } from "../../lib/sessionExecutionHome";
import { normalizeSessionOriginLabel } from "../../lib/sessionWorkspace";
import {
  formatRelativeTime,
  getRuntimeMetaLabel,
  getCardRuntimePhaseLabel,
  getRuntimeDisplayCopy,
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
// ProviderIcon
// ---------------------------------------------------------------------------

function ProviderIcon({ provider }: { provider: string }) {
  const color = getProviderColor(provider);
  const svgProps = { width: 14, height: 14, viewBox: "0 0 24 24", fill: "none", "aria-hidden": true as const, style: { color, flexShrink: 0 } };

  switch (provider) {
    case "claude":
      return (
        <svg {...svgProps}>
          <path d="M12 2l2.4 7.2L22 12l-7.6 2.8L12 22l-2.4-7.2L2 12l7.6-2.8z" fill="currentColor" />
        </svg>
      );
    case "codex":
      return (
        <svg {...svgProps}>
          <path d="M12 2a2.5 2.5 0 010 5 2.5 2.5 0 01-4.33 2.5A2.5 2.5 0 012 12a2.5 2.5 0 015.67 2.5A2.5 2.5 0 0112 17a2.5 2.5 0 014.33 2.5A2.5 2.5 0 0122 12a2.5 2.5 0 01-5.67-2.5A2.5 2.5 0 0112 7a2.5 2.5 0 010-5z" fill="currentColor" opacity="0.9" />
        </svg>
      );
    case "gemini":
      return (
        <svg {...svgProps}>
          <path d="M12 2C12 10 14 12 22 12C14 12 12 14 12 22C12 14 10 12 2 12C10 12 12 10 12 2Z" fill="currentColor" />
        </svg>
      );
    case "zai":
      return (
        <svg {...svgProps}>
          <path d="M6 6h12v2.5L8.5 18H18v2H6v-2.5L15.5 8H6z" fill="currentColor" />
        </svg>
      );
    default:
      return (
        <svg {...svgProps} stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="4 17 10 11 4 5" />
          <line x1="12" y1="19" x2="20" y2="19" />
        </svg>
      );
  }
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
  const cardCapabilityLabel =
    interaction.mode === "managed_local_unavailable"
      ? "Browser control offline"
      : null;
  const turnCount = session.user_messages;
  const toolCount = session.tool_calls;
  const runtime = resolveSessionRuntimeState(session);
  const runtimeMetaLabel = getRuntimeMetaLabel(runtime);
  const runtimeDisplay = getRuntimeDisplayCopy(runtime, {
    managedLocal: interaction.isManagedLocalSession,
  });
  const runtimePhaseLabel = interaction.isManagedLocalSession
    ? runtimeDisplay.headline
    : getCardRuntimePhaseLabel(runtime);

  const projectLabel = getProjectLabel(session);
  const title = getSessionTitle(session);
  const homeLabel = normalizeExecutionVenueLabel(session.home_label);
  const runtimeHostLabel =
    session.control?.source_runner_name?.trim() ||
    homeLabel ||
    interaction.sourceOriginLabel ||
    "host";
  const cardRuntimeMetaParts = interaction.isManagedLocalSession
    ? [
        runtimeDisplay.detail,
        interaction.liveControlAvailable
          ? `Live on ${runtimeHostLabel}`
          : "Browser control offline",
        runtimeMetaLabel && runtimeMetaLabel !== "Live on host"
          ? runtimeMetaLabel
          : null,
      ].filter(Boolean)
    : [runtimeMetaLabel].filter(Boolean);
  const headOriginLabel = normalizeSessionOriginLabel(thread.head_origin_label);
  const startedOriginLabel = normalizeSessionOriginLabel(thread.started_origin_label);
  const showHeadOriginLabel =
    !compatibilityMode && !!headOriginLabel && headOriginLabel !== homeLabel;
  const showStartedOriginLabel =
    !compatibilityMode &&
    thread.continuation_count > 1 &&
    !!startedOriginLabel &&
    startedOriginLabel !== headOriginLabel;
  const showContinuationCount = !compatibilityMode && thread.continuation_count > 1;
  const inlineHeadOriginLabel = showHeadOriginLabel && !showStartedOriginLabel && !showContinuationCount;
  const showIdentitySecondary =
    !inlineHeadOriginLabel && (showHeadOriginLabel || showStartedOriginLabel || showContinuationCount);
  const showManagementPill = !interaction.isManagedLocalSession;
  const showStatusRow = runtime.hasSignal || showManagementPill || !!cardCapabilityLabel;

  const showKeywordSnippet = !isSemanticResult && !!highlightQuery && !!detailSession.match_snippet;
  const showSemanticSnippet = isSemanticResult && !!detailSession.match_snippet;
  const showSummary = !showKeywordSnippet && !showSemanticSnippet && !!session.summary;
  const showGenerating = !showKeywordSnippet && !showSemanticSnippet && !session.summary && !session.summary_title;
  const cardActionLabel = compatibilityMode ? "Open match" : "Open session";
  const cardClassName = [
    "session-card",
    confirming ? "session-card--confirming" : "",
    runtime.isExecuting ? "session-card--live" : "",
    runtime.isIdle ? "session-card--idle" : "",
    runtime.tone === "inferred" ? "session-card--inferred" : "",
    runtime.tone === "thinking" ? "session-card--thinking" : "",
    runtime.tone === "running" ? "session-card--running" : "",
    runtime.tone === "needs-user" ? "session-card--needs-user" : "",
    runtime.tone === "blocked" ? "session-card--blocked" : "",
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
      style={{ borderLeftColor: getProviderColor(session.provider) }}
      data-testid="session-card"
      data-session-id={detailSession.id}
      data-thread-id={thread.thread_id}
      data-runtime-tone={runtime.tone}
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
                <ProviderIcon provider={session.provider} />
                <span className="provider-name" style={{ color: getProviderColor(session.provider) }}>{session.provider}</span>
              </span>
              {session.git_branch && (
                <span className="session-card-branch-badge">
                  <span className="branch-icon">&#x2387;</span>
                  {session.git_branch}
                </span>
              )}
              {homeLabel && (
                <span className="environment-badge">
                  {homeLabel}
                </span>
              )}
              {inlineHeadOriginLabel && (
                <span className="environment-badge environment-badge--secondary">Head: {headOriginLabel}</span>
              )}
            </div>

            {showIdentitySecondary && (
              <div className="session-card-identity-secondary">
                {showHeadOriginLabel && (
                  <span className="environment-badge environment-badge--secondary">Head: {headOriginLabel}</span>
                )}
                {showStartedOriginLabel && (
                  <span className="environment-badge environment-badge--secondary">Started: {startedOriginLabel}</span>
                )}
                {showContinuationCount && (
                  <span className="environment-badge environment-badge--secondary">
                    {thread.continuation_count} continuations
                  </span>
                )}
              </div>
            )}
          </div>

          {showStatusRow && (
            <div className="session-card-status">
              {runtime.hasSignal && (
                <div className={`session-card-runtime session-card-runtime--${runtime.tone}`}>
                  <PresenceBadge
                    state={runtime.presenceState}
                    tool={runtime.presenceTool}
                    compact
                    heuristicActive={runtime.heuristicActive}
                    showUnknown={runtime.truthTier === "stale"}
                  />
                  <span className="session-card-runtime-phase">{runtimePhaseLabel}</span>
                  {cardRuntimeMetaParts.length > 0 && (
                    <span className="session-card-runtime-meta">
                      {cardRuntimeMetaParts.join(" • ")}
                    </span>
                  )}
                </div>
              )}
              {showManagementPill ? (
                <span
                  className={`session-card-management-pill session-card-management-pill--${interaction.managementVariant}`}
                  data-testid="session-card-management"
                  title={interaction.managementDescription}
                >
                  {interaction.managementLabel}
                </span>
              ) : null}
              {cardCapabilityLabel ? (
                <span
                  className={`session-card-capability-pill session-card-capability-pill--${interaction.capabilityVariant}`}
                  data-testid="session-card-capability"
                  title={interaction.capabilityDescription ?? undefined}
                >
                  {cardCapabilityLabel}
                </span>
              ) : null}
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
                  {compatibilityMode
                    ? `Matched ${formatRelativeTime(detailSession.started_at, relativeNowMs)}`
                    : `Started ${formatRelativeTime(thread.root.started_at, relativeNowMs)}`}
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
