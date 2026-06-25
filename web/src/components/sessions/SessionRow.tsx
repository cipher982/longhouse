/**
 * SessionRow — inbox-style row for a single session thread.
 *
 * Reserved geometry: the right metadata cluster has fixed width and
 * fixed height, so status/time changes never reflow the row. State
 * transitions (idle → thinking → idle) crossfade in place.
 */

import { useCallback, useEffect, useRef, type CSSProperties, type ReactNode, type Ref } from "react";
import type { DraggableAttributes } from "@dnd-kit/core";
import type { SyntheticListenerMap } from "@dnd-kit/core/dist/hooks/utilities";
import { type SessionCapabilities, type TimelineSessionCard } from "../../services/api/agents";
import { isSessionClosed, resolveTimelineSignal, timelineSignalLabel } from "../../lib/sessionRuntime";
import {
  formatRelativeTime,
  getBranchLabel,
  getDriftTitle,
  getSessionCardText,
  renderHighlightedText,
} from "../../lib/sessionUtils";
import { ProviderGlyph } from "../ProviderGlyph";
import { getProviderLabel } from "../../lib/providers";

const HOVER_PREFETCH_DELAY_MS = 180;

type RowControlTone = "live" | "reattach" | "observe" | "search";

export interface RowControlPresentation {
  label: string;
  tone: RowControlTone;
  title: string;
}

export interface SessionRowProps {
  thread: TimelineSessionCard;
  onClick: () => void;
  onPrefetch?: () => void;
  allowHoverPrefetch?: () => boolean;
  highlightQuery?: string;
  relativeNowMs: number;
  closed?: boolean;
  /** True when this row is currently being dragged (visual hint). */
  dragging?: boolean;
  /** dnd-kit `setNodeRef`. */
  forwardedRef?: Ref<HTMLButtonElement>;
  /** dnd-kit transform/transition style. */
  style?: CSSProperties;
  /** dnd-kit attributes (role, aria-roledescription, etc). */
  sortableAttributes?: DraggableAttributes;
  /** dnd-kit listeners (pointer/keyboard activators). Spread onto the row. */
  sortableListeners?: SyntheticListenerMap;
}

export function SessionRow({
  thread,
  onClick,
  onPrefetch,
  allowHoverPrefetch,
  relativeNowMs,
  highlightQuery,
  closed = false,
  dragging = false,
  forwardedRef,
  style,
  sortableAttributes,
  sortableListeners,
}: SessionRowProps) {
  const session = thread.head;
  const detailSession = thread.detail;
  const timelineStatus = session.timeline_card.status;
  const isClosed = closed || isCardClosed(thread);
  const text = getSessionCardText(session, { titleMaxChars: 96, subheadingMaxChars: 200 });
  const branch = getBranchLabel(session.git_branch);
  const provider = session.provider;
  const control = getRowControlPresentation(session.capabilities);
  const startedAtIso = thread.root?.started_at || session.started_at;
  const timeLabel = getRowTimeLabel({
    seenAt: timelineStatus.seen_at,
    seenAtPrefix: timelineStatus.seen_at_prefix,
    startedAt: startedAtIso,
    relativeNowMs,
  });

  const statusTone = isClosed ? "closed" : timelineStatus.tone;
  const statusLabel = isClosed ? "closed" : timelineStatus.label;
  // 3-stop attention signal (amber=waiting / teal=working / grey=quiet), shared
  // with iOS. Drives the dot color + the a11y label so amber isn't sight-only.
  const signal = resolveTimelineSignal(session);

  // When the user is searching and the backend returned a match snippet,
  // show that as the row's secondary line with the query highlighted.
  const matchSnippet = detailSession?.match_snippet ?? null;
  const showSnippet = !!highlightQuery && !!matchSnippet;
  // B-lite drift line: while actively working, the live (drifting) summary title
  // is parked on the demoted secondary line as "now: …", where movement is
  // legitimate. The frozen headline above never moves (muscle memory). Suppressed
  // when the drift would just echo the headline.
  const driftTitle = getDriftTitle(session, text.title);
  const summary: ReactNode = showSnippet
    ? renderHighlightedText(matchSnippet!, highlightQuery!)
    : signal === "working" && driftTitle
      ? `now: ${driftTitle}`
      : text.subheading;

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
      ref={forwardedRef}
      type="button"
      className="inbox-row"
      data-testid="session-row"
      data-session-id={session.id}
      data-thread-id={thread.thread_id}
      data-status={statusTone}
      data-closed={isClosed ? "true" : "false"}
      data-dragging={dragging ? "true" : undefined}
      style={style}
      {...(sortableAttributes ?? {})}
      {...(sortableListeners ?? {})}
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

      <span className="inbox-row-status" aria-hidden="false">
        <span
          className="inbox-row-status-dot"
          data-tone={statusTone}
          data-signal={signal}
          aria-label={timelineSignalLabel(signal)}
        />
        <span className="inbox-row-status-label">{statusLabel}</span>
        <span
          className="inbox-row-control inbox-row-control--status"
          data-tone={control.tone}
          data-testid="session-row-control-mobile"
          title={control.title}
          aria-label={control.title}
        >
          {control.label}
        </span>
      </span>
      <span className="inbox-row-source">
        <span
          className="inbox-row-control inbox-row-control--source"
          data-tone={control.tone}
          data-testid="session-row-control"
          title={control.title}
          aria-label={control.title}
        >
          {control.label}
        </span>
        <span className="inbox-row-provider" title={getProviderLabel(provider)}>
          <ProviderGlyph provider={provider} size={18} />
          <span className="inbox-row-provider-name">{getProviderLabel(provider)}</span>
        </span>
        {branch ? <span className="inbox-row-branch">{branch}</span> : null}
      </span>
      <span className="inbox-row-time">
        {timeLabel}
      </span>
    </button>
  );
}

export function getRowControlPresentation(capabilities: SessionCapabilities | null | undefined): RowControlPresentation {
  if (capabilities?.live_control_available || capabilities?.control_label === "live") {
    return {
      label: "Live control",
      tone: "live",
      title: "Managed session with live control available",
    };
  }

  if (capabilities?.host_reattach_available || capabilities?.control_label === "reattach") {
    return {
      label: "Reattach",
      tone: "reattach",
      title: "Managed session can be reattached from its host",
    };
  }

  if (capabilities?.observe_only || capabilities?.control_label === "search-only") {
    return {
      label: "Observe only",
      tone: "observe",
      title: "Transcript output is observable, but this session is not steerable",
    };
  }

  return {
    label: "Search only",
    tone: "search",
    title: "Imported transcript is searchable, but this session is not steerable",
  };
}

export function getRowTimeLabel({
  seenAt,
  seenAtPrefix,
  startedAt,
  relativeNowMs,
}: {
  seenAt: string | null;
  seenAtPrefix: string | null;
  startedAt: string | null;
  relativeNowMs: number;
}): string {
  if (seenAt) {
    const prefix = seenAtPrefix?.trim() || "Updated";
    return `${prefix} ${formatRelativeTime(seenAt, relativeNowMs)}`;
  }
  if (startedAt) {
    return `Started ${formatRelativeTime(startedAt, relativeNowMs)}`;
  }
  return "";
}

function isCardClosed(card: TimelineSessionCard): boolean {
  const session = card.head;
  const status = session?.timeline_card?.status;
  if (status?.tone === "closed") return true;
  return isSessionClosed(session);
}
