/**
 * Item 7 (web-restyle-signal.md polish pass): a left-hand column listing the
 * session's turns, shown only on wide viewports (see the
 * `.session-turn-outline*` CSS block in session-workspace.css, deletable together
 * with this file in one commit). Turns derive from the loaded thread that's
 * already fetched — no new requests.
 */
import { useEffect, useRef } from "react";
import type { TimelineItem } from "@/shared/session/model";
import { formatTime } from "@/shared/session/model";
import { parseUTC } from "@/shared/lib/dateUtils";
import type { AgentEventId } from "@/shared/api/agents";
import { cleanPromptPreview } from "@/shared/session/promptPreview";

/** DOM id prefix of a transcript row: `event-<eventId>`. */
export const TURN_ROW_ID_PREFIX = "event-";

export function turnRowId(eventId: AgentEventId | string): string {
  return `${TURN_ROW_ID_PREFIX}${eventId}`;
}

export function turnKeyForEventId(eventId: AgentEventId | string): string {
  return `turn-${eventId}`;
}

/**
 * "8:39" rather than "08:39 AM": an outline row spends its width on the ask.
 * The day period is dropped; the row's tooltip carries the full time.
 */
export function formatTurnTime(dateStr: string): string {
  const parts = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" }).formatToParts(
    parseUTC(dateStr),
  );
  const hour = parts.find((part) => part.type === "hour")?.value;
  const minute = parts.find((part) => part.type === "minute")?.value;
  return hour && minute ? `${hour}:${minute}` : formatTime(dateStr);
}

/** How much of the user's ask shows on one outline row. */
const ASK_PREVIEW_LENGTH = 48;

export interface TurnOutlineTurn {
  /** Stable per-turn key — also usable as the React list key. */
  key: string;
  /** The originating user message's event id — turns rowId `event-<id>`. */
  eventId: AgentEventId;
  timestamp: string;
  /** First 48 characters of the user's ask, as a one-line preview. */
  askPreview: string;
}

/**
 * Each user message in the loaded thread starts a turn. Pure and
 * side-effect free so it's trivially unit-testable against a fixture thread
 * without rendering anything.
 */
export function deriveTurnOutline(items: TimelineItem[]): TurnOutlineTurn[] {
  const turns: TurnOutlineTurn[] = [];
  for (const item of items) {
    if (item.kind !== "message" || item.event.role !== "user") continue;
    const raw = cleanPromptPreview(item.event.content_text);
    turns.push({
      key: turnKeyForEventId(item.event.id),
      eventId: item.event.id,
      timestamp: item.event.timestamp,
      askPreview: raw.slice(0, ASK_PREVIEW_LENGTH),
    });
  }
  return turns;
}

export interface TurnOutlineProps {
  turns: TurnOutlineTurn[];
  /** The live/running turn's key, when the session has one; marked with the
   * ember instead of a plain time. */
  runningTurnKey?: string | null;
  /** The turn to visually highlight as "here" in the transcript. */
  currentTurnKey?: string | null;
  /** Scrolls the transcript to this turn's user-message row — the caller
   * owns the actual scroll (SessionDetailPage: `event-<eventId>`). */
  onSelectTurn: (turn: TurnOutlineTurn) => void;
}

export function TurnOutline({
  turns,
  runningTurnKey = null,
  currentTurnKey = null,
  onSelectTurn,
}: TurnOutlineProps) {
  const navRef = useRef<HTMLElement | null>(null);

  // Keep the current row inside the outline's own scroll viewport as the
  // transcript scrolls (long sessions have more turns than the column fits).
  useEffect(() => {
    const nav = navRef.current;
    if (!nav) return;
    const reveal = () => {
      const row = nav.querySelector<HTMLElement>('[aria-current="true"]');
      if (!row) return;
      const navBox = nav.getBoundingClientRect();
      const rowBox = row.getBoundingClientRect();
      if (rowBox.top < navBox.top) nav.scrollTop -= navBox.top - rowBox.top;
      else if (rowBox.bottom > navBox.bottom) nav.scrollTop += rowBox.bottom - navBox.bottom;
    };
    reveal();
    // The column is hidden below 1600px and resizes with the window: reveal
    // again when it gains a height.
    if (typeof ResizeObserver === "undefined") return;
    const resizes = new ResizeObserver(reveal);
    resizes.observe(nav);
    return () => resizes.disconnect();
  }, [currentTurnKey, turns.length]);

  if (turns.length === 0) return null;

  return (
    <nav ref={navRef} className="session-turn-outline" data-testid="session-turn-outline" aria-label="Turns">
      <ol className="session-turn-outline__list">
        {turns.map((turn) => {
          const isRunning = turn.key === runningTurnKey;
          const isCurrent = turn.key === currentTurnKey;
          return (
            <li key={turn.key}>
              <button
                type="button"
                className={`session-turn-outline__item${isCurrent ? " is-current" : ""}${isRunning ? " is-running" : ""}`}
                data-testid="session-turn-outline-item"
                aria-current={isCurrent ? "true" : undefined}
                onClick={() => onSelectTurn(turn)}
                title={`${formatTime(turn.timestamp)} · ${turn.askPreview || "(empty message)"}`}
              >
                {isRunning ? <span className="session-turn-outline__ember" aria-hidden="true" /> : null}
                <span className="session-turn-outline__time">{formatTurnTime(turn.timestamp)}</span>
                <span className="session-turn-outline__ask">{turn.askPreview || "(empty message)"}</span>
              </button>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
