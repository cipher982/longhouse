/**
 * SessionRow — inbox-style row for a single session thread.
 *
 * Reserved geometry: the right metadata cluster has fixed width and
 * fixed height, so status/time changes never reflow the row. State
 * transitions (idle → thinking → idle) crossfade in place.
 */

import { useCallback, useEffect, useRef, useState, type CSSProperties, type ReactNode, type Ref } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "react-hot-toast";
import { setSessionTimelineVisibility } from "../../services/api/agents";
import type { DraggableAttributes } from "@dnd-kit/core";
import type { SyntheticListenerMap } from "@dnd-kit/core/dist/hooks/utilities";
import { getTimelineSessionAnchor, type SessionStateFacts, type TimelineSessionCard } from "../../services/api/agents";
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

type RowControlTone = "live" | "reattach" | "observe" | "degraded" | "search";

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
  /** Unread band row: unacknowledged Console result (email semantics). */
  unread?: boolean;
  /** True when this row is currently being dragged (visual hint). */
  dragging?: boolean;
  /** dnd-kit `setNodeRef`. */
  forwardedRef?: Ref<HTMLDivElement | HTMLButtonElement>;
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
  unread = false,
  dragging = false,
  forwardedRef,
  style,
  sortableAttributes,
  sortableListeners,
}: SessionRowProps) {
  const session = thread.head;
  const detailSession = thread.detail;
  const timelineStatus = session.session_state.presentation.primary;
  const isClosed = closed || isCardClosed(thread);
  const text = getSessionCardText(session, { titleMaxChars: 96, subheadingMaxChars: 200 });
  const branch = getBranchLabel(session.git_branch);
  const provider = session.provider;
  const control = getRowControlPresentation(session.session_state);
  const startedAtIso = thread.root?.started_at || session.started_at;
  // Unread rows label by result completion, not generic activity: the band is
  // "results waiting for you" and the row says what landed and when.
  const unreadOutcome = session.session_state.last_result_outcome;
  const unreadOutcomeLabel = unreadOutcome === "failed" ? "Failed" : unreadOutcome === "cancelled" ? "Cancelled" : "Finished";
  const timeLabel = getRowTimeLabel({
    seenAt: unread ? (session.session_state.last_result_at ?? null) : getTimelineSessionAnchor(session),
    seenAtPrefix: unread ? unreadOutcomeLabel : "Updated",
    startedAt: startedAtIso,
    relativeNowMs,
  });

  const statusTone = unread ? (unreadOutcome === "failed" ? "blocked" : "idle") : isClosed ? "closed" : (timelineStatus?.tone ?? "inactive");
  const statusLabel = unread ? unreadOutcomeLabel : isClosed ? "Closed" : (timelineStatus?.label ?? "");
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
  const queryClient = useQueryClient();
  const [hiding, setHiding] = useState(false);

  const isUserHidden = session.user_hidden_from_timeline === true;
  const isSystemHidden = session.hidden_from_default_timeline === true;
  const isHidden = isUserHidden || isSystemHidden;

  const handleToggleVisibility = useCallback(
    async (e: React.MouseEvent) => {
      e.stopPropagation();
      e.preventDefault();
      if (hiding) return;
      setHiding(true);
      const nextHidden = !isHidden;
      try {
        await setSessionTimelineVisibility(session.id, nextHidden);
        queryClient.invalidateQueries({ queryKey: ["agent-sessions"] });
        if (nextHidden) {
          toast(
            (t) => (
              <span className="session-hide-toast">
                <span>Session hidden from timeline</span>
                <button
                  type="button"
                  className="session-hide-undo-btn"
                  onClick={async (event) => {
                    event.stopPropagation();
                    toast.dismiss(t.id);
                    await setSessionTimelineVisibility(session.id, false);
                    queryClient.invalidateQueries({ queryKey: ["agent-sessions"] });
                    toast.success("Restored session to timeline");
                  }}
                >
                  Undo
                </button>
              </span>
            ),
            { duration: 5000 },
          );
        } else {
          toast.success("Session restored to timeline");
        }
      } catch {
        toast.error("Failed to update session visibility");
      } finally {
        setHiding(false);
      }
    },
    [hiding, isHidden, queryClient, session.id],
  );

  return (
    <div
      ref={forwardedRef as Ref<HTMLDivElement>}
      className="inbox-row"
      data-testid="session-row"
      data-session-id={session.id}
      data-thread-id={thread.thread_id}
      data-status={statusTone}
      data-activity-state={session.session_state.activity.state}
      data-activity-observed-at={session.session_state.activity.observed_at ?? undefined}
      data-state-commit-seq={session.session_state.commit_seq ?? undefined}
      data-closed={isClosed ? "true" : "false"}
      data-user-hidden={isHidden ? "true" : undefined}
      data-unread={unread ? "true" : undefined}
      data-dragging={dragging ? "true" : undefined}
      style={style}
      {...(sortableAttributes ?? { role: "button", tabIndex: 0 })}
      {...(sortableListeners ?? {})}
      onClick={onClick}
      onKeyDown={(e) => {
        if (e.target === e.currentTarget && (e.key === "Enter" || e.key === " ")) {
          e.preventDefault();
          onClick();
        }
      }}
      onMouseEnter={scheduleHover}
      onMouseLeave={clearHover}
      onFocus={() => {
        clearHover();
        onPrefetch?.();
      }}
      onBlur={clearHover}
    >
      <div className="inbox-row-main">
        <div
          className="inbox-row-title"
          {...{ elementtiming: "longhouse-session-row" }}
        >
          {isHidden && <span className="inbox-row-hidden-badge">{isUserHidden ? "hidden" : "auto"}</span>}
          {text.title}
        </div>
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
        <span className="inbox-row-time-text">{timeLabel}</span>
        <button
          type="button"
          className="inbox-row-hide-btn"
          title={isHidden ? "Restore to timeline" : "Hide from timeline"}
          aria-label={isHidden ? "Restore to timeline" : "Hide from timeline"}
          data-testid="session-row-hide-button"
          onClick={handleToggleVisibility}
        >
          {isHidden ? (
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
              <circle cx="12" cy="12" r="3" />
            </svg>
          ) : (
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24" />
              <line x1="1" y1="1" x2="23" y2="23" />
            </svg>
          )}
        </button>
      </span>
    </div>
  );
}

export function getRowControlPresentation(facts: SessionStateFacts): RowControlPresentation {
  const access = facts.presentation.access;
  // The server drops the access label for a Helm session whose run has ended,
  // because access and continuation are separate axes. Falling through to
  // "Imported transcript ... not steerable" would misdescribe a managed session
  // that Longhouse owns and can resume.
  if (!access && facts.mode === "helm" && facts.run?.lifecycle === "ended") {
    return {
      label: "Ended",
      tone: "search",
      title: "This managed session's run has ended",
    };
  }
  if (!access) return searchOnlyPresentation();
  // `machine_offline` is the one access state that is an outage rather than a
  // capability statement, so it must not share the muted tone that means
  // "searchable history". Every other key stays quiet by design.
  const tone: RowControlTone = access.key === "live_control"
    ? "live"
    : access.key === "reattach"
      ? "reattach"
      : access.key === "machine_offline"
        ? "degraded"
        : access.key === "observe_only"
          ? "observe"
          : "search";
  return {
    label: access.label,
    tone,
    title: access.label,
  };
}

function liveControlPresentation(): RowControlPresentation {
  return {
    label: "Live control",
    tone: "live",
    title: "Managed session with live control available",
  };
}

function reattachControlPresentation(): RowControlPresentation {
  return {
    label: "Reattach",
    tone: "reattach",
    title: "Managed session can be reattached from its host",
  };
}

function observeOnlyPresentation(): RowControlPresentation {
  // Kernel "search-only" covers observe-only tails: readable transcript output,
  // but no steerable control path.
  return {
    label: "Observe only",
    tone: "observe",
    title: "Transcript output is observable, but this session is not steerable",
  };
}

function searchOnlyPresentation(): RowControlPresentation {
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
  return isSessionClosed(card.head);
}
