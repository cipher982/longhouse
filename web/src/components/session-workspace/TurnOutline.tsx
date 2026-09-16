/**
 * Item 7 (web-restyle-signal.md polish pass): a left-hand column listing the
 * session's turns, shown only on wide viewports (see the
 * `.session-turn-outline*` CSS block in session-workspace.css, deletable together
 * with this file in one commit). Turns derive from the loaded thread that's
 * already fetched — no new requests.
 */
import type { TimelineItem } from "../../lib/sessionWorkspace";
import { formatTime } from "../../lib/sessionWorkspace";
import type { AgentEventId } from "../../services/api/agents";

/** How much of the user's ask shows on one outline row. */
const ASK_PREVIEW_LENGTH = 48;

export interface TurnOutlineTurn {
  /** Stable per-turn key — also usable as the React list key. */
  key: string;
  /** The originating user message's event id — turns rowId `event-<id>`. */
  eventId: AgentEventId;
  timestamp: string;
  /** First 48 characters of the user's ask, whitespace-collapsed. */
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
    const raw = (item.event.content_text ?? "").replace(/\s+/g, " ").trim();
    turns.push({
      key: `turn-${item.event.id}`,
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
  if (turns.length === 0) return null;

  return (
    <nav className="session-turn-outline" data-testid="session-turn-outline" aria-label="Turns">
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
              >
                {isRunning ? <span className="session-turn-outline__ember" aria-hidden="true" /> : null}
                <span className="session-turn-outline__time">{formatTime(turn.timestamp)}</span>
                <span className="session-turn-outline__ask">{turn.askPreview || "(empty message)"}</span>
              </button>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
