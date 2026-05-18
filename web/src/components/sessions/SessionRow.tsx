/**
 * SessionRow — inbox-style row for a single session thread.
 *
 * Reserved geometry: the right metadata cluster has fixed width and
 * fixed height, so status/time changes never reflow the row. State
 * transitions (idle → thinking → idle) crossfade in place.
 */

import { useCallback, useEffect, useRef, type ReactNode } from "react";
import { type TimelineSessionCard } from "../../services/api/agents";
import {
  isSessionClosed,
  resolveSessionRuntimeState,
} from "../../lib/sessionRuntime";
import {
  formatRelativeTime,
  getBranchLabel,
  getSessionCardText,
  renderHighlightedText,
} from "../../lib/sessionUtils";

const HOVER_PREFETCH_DELAY_MS = 180;

export interface SessionRowProps {
  thread: TimelineSessionCard;
  onClick: () => void;
  onPrefetch?: () => void;
  allowHoverPrefetch?: () => boolean;
  highlightQuery?: string;
  relativeNowMs: number;
  closed?: boolean;
}

export function SessionRow({
  thread,
  onClick,
  onPrefetch,
  allowHoverPrefetch,
  relativeNowMs,
  highlightQuery,
  closed = false,
}: SessionRowProps) {
  const session = thread.head;
  const detailSession = thread.detail;
  const runtime = resolveSessionRuntimeState(session);
  const cardStatus = session.timeline_card?.status ?? runtime.factStatus ?? null;
  const isClosed = closed || isCardClosed(thread);
  const text = getSessionCardText(session, { titleMaxChars: 96, subheadingMaxChars: 200 });
  const branch = getBranchLabel(session.git_branch);
  const provider = session.provider;
  const startedAtIso = thread.timeline_anchor_at || thread.root?.started_at || session.started_at;

  // When the user is searching and the backend returned a match snippet,
  // show that as the row's secondary line with the query highlighted.
  const matchSnippet = detailSession?.match_snippet ?? null;
  const showSnippet = !!highlightQuery && !!matchSnippet;
  const summary: ReactNode = showSnippet
    ? renderHighlightedText(matchSnippet!, highlightQuery!)
    : text.subheading;

  const statusTone = isClosed ? "closed" : (cardStatus?.tone ?? runtime.tone);
  const statusLabel = isClosed
    ? "closed"
    : (cardStatus?.label ?? humanizeTone(runtime.tone));

  const hoverTimerRef = useRef<number | null>(null);
  const clearHover = useCallback(() => {
    if (hoverTimerRef.current != null) {
      window.clearTimeout(hoverTimerRef.current);
      hoverTimerRef.current = null;
    }
  }, []);
  useEffect(() => clearHover, [clearHover]);

  const scheduleHover = useCallback(() => {
    if (!onPrefetch) return;
    clearHover();
    hoverTimerRef.current = window.setTimeout(() => {
      hoverTimerRef.current = null;
      if (allowHoverPrefetch && !allowHoverPrefetch()) return;
      onPrefetch();
    }, HOVER_PREFETCH_DELAY_MS);
  }, [allowHoverPrefetch, clearHover, onPrefetch]);

  return (
    <button
      type="button"
      className="inbox-row"
      data-testid="session-row"
      data-session-id={session.id}
      data-thread-id={thread.thread_id}
      data-status={statusTone}
      data-closed={isClosed ? "true" : "false"}
      onClick={onClick}
      onMouseEnter={scheduleHover}
      onMouseLeave={clearHover}
      onFocus={() => {
        clearHover();
        onPrefetch?.();
      }}
      onBlur={clearHover}
    >
      <div className="inbox-row-main">
        <div className="inbox-row-title">{text.title}</div>
        {summary ? (
          <div
            className={`inbox-row-summary${showSnippet ? " inbox-row-summary--snippet" : ""}`}
            data-testid={showSnippet ? "session-row-snippet" : undefined}
          >
            {summary}
          </div>
        ) : (
          <div className="inbox-row-summary inbox-row-summary--empty" aria-hidden="true">
            &nbsp;
          </div>
        )}
      </div>

      <div className="inbox-row-meta" aria-hidden="false">
        <div className="inbox-row-meta-line inbox-row-meta-provider">
          <span className="inbox-row-provider">{provider}</span>
          {branch ? <span className="inbox-row-branch">{branch}</span> : null}
        </div>
        <div className="inbox-row-meta-line inbox-row-meta-status">
          <span
            className="inbox-row-status-dot"
            data-tone={statusTone}
            aria-hidden="true"
          />
          <span className="inbox-row-status-label">{statusLabel}</span>
          <span className="inbox-row-time">
            {startedAtIso ? formatRelativeTime(startedAtIso, relativeNowMs) : ""}
          </span>
        </div>
      </div>
    </button>
  );
}

function humanizeTone(tone: string): string {
  switch (tone) {
    case "thinking":
      return "thinking";
    case "running":
      return "running";
    case "idle":
      return "idle";
    case "blocked":
      return "blocked";
    case "stalled":
      return "stalled";
    case "active":
      return "active";
    case "closed":
      return "closed";
    default:
      return "";
  }
}

function isCardClosed(card: TimelineSessionCard): boolean {
  const session = card.head;
  const status = session?.timeline_card?.status;
  if (status?.tone === "closed" || status?.label === "Closed") return true;
  return isSessionClosed(session);
}
