/**
 * Right-edge turn minimap (tick marks) with hover/click popover and optional
 * pinned right drawer. Keeps the left session rail free of expanding accordions
 * while preserving one-click jumping across all turns in the session.
 *
 * Turns come from the server's turn index merged with the loaded thread
 * (`mergeTurnOutline`).
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { TimelineItem } from "@/shared/session/model";
import { formatTime } from "@/shared/session/model";
import { parseUTC } from "@/shared/lib/dateUtils";
import type { AgentEventId, SessionTurn } from "@/shared/api/agents";
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
 * "13:39" (24-hour, zero-padded): an outline row spends its width on the ask,
 * and a 12-hour clock without a day period would be ambiguous.
 */
export function formatTurnTime(dateStr: string): string {
  const date = parseUTC(dateStr);
  if (Number.isNaN(date.getTime())) return formatTime(dateStr);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** How much of the user's ask is kept for outline rows (CSS ellipsis trims to fit). */
const ASK_PREVIEW_LENGTH = 96;

/** Upper bound on visible tick marks along the right margin so 100+ turn
 * sessions never overflow the transcript height. The popover/drawer always
 * lists every turn. */
export const MAX_TICK_MARKS = 36;

/** Brief hover-intent delay so incidental pointer sweeps past the right edge
 * do not throw the popover over the transcript. */
export const HOVER_OPEN_DELAY_MS = 120;

/** Hover grace window so moving diagonally from the tick rail to the card does
 * not flicker closed. */
const HOVER_CLOSE_DELAY_MS = 180;

/** Line-height multiplier when normalizing Firefox DOM_DELTA_LINE wheel events. */
const WHEEL_LINE_PX = 16;

export interface TurnOutlineTurn {
  /** Stable per-turn key — also usable as the React list key. */
  key: string;
  /** The originating user message's event id — turns rowId `event-<id>`. */
  eventId: AgentEventId;
  timestamp: string;
  /** First 96 characters of the user's ask, as a one-line preview. */
  askPreview: string;
}

export interface TurnTickMark {
  index: number;
  isPast: boolean;
  isCurrent: boolean;
  isRunning: boolean;
}

/**
 * Map `turns` onto at most `MAX_TICK_MARKS` visual ticks while preserving
 * exact active and running positions.
 */
export function buildTickMarks(
  turnCount: number,
  activeIndex: number,
  runningIndex: number,
  maxTicks: number = MAX_TICK_MARKS,
): TurnTickMark[] {
  if (turnCount <= 0) return [];
  const count = Math.min(turnCount, Math.max(1, maxTicks));
  const mapIndex = (idx: number): number => {
    if (idx < 0 || idx >= turnCount) return -1;
    if (count === turnCount || turnCount <= 1) return idx;
    return Math.round((idx / (turnCount - 1)) * (count - 1));
  };
  const activeTick = mapIndex(activeIndex);
  const runningTick = mapIndex(runningIndex);
  return Array.from({ length: count }, (_, index) => ({
    index,
    isCurrent: index === activeTick,
    isPast: activeTick >= 0 && index < activeTick,
    isRunning: index === runningTick,
  }));
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

const PROMPT_FRAGMENT = /^(?:image|text)-\d+$/;

/** Whether `eventId` is one content block of the prompt `baseId` (timelineModel merges these). */
function isPromptFragmentOf(baseId: string, eventId: string): boolean {
  const prefix = `${baseId}-`;
  return eventId.startsWith(prefix) && PROMPT_FRAGMENT.test(eventId.slice(prefix.length));
}

/** An outline row for a turn the server's turn index returned. */
export function turnFromIndex(turn: SessionTurn): TurnOutlineTurn {
  return {
    key: turnKeyForEventId(turn.event_id),
    eventId: turn.event_id,
    timestamp: turn.timestamp,
    askPreview: cleanPromptPreview(turn.preview).slice(0, ASK_PREVIEW_LENGTH),
  };
}

/**
 * The whole session's turns: the server's turn index for history that is not
 * loaded, and the loaded thread for the window it covers.
 *
 * `loadedFromMs` is the time of the oldest loaded event: index turns older
 * than it are kept, and inside the loaded window the thread wins, so a turn a
 * rewind just removed or a new one the index has not seen yet is shown as the
 * transcript shows it. Pass `-Infinity` when the whole session is loaded and
 * `Infinity` while nothing is.
 */
export function mergeTurnOutline(
  indexed: readonly SessionTurn[] | null | undefined,
  loaded: TurnOutlineTurn[],
  loadedFromMs: number,
): TurnOutlineTurn[] {
  if (!indexed || indexed.length === 0) return loaded;
  const loadedIds = new Set(loaded.map((turn) => String(turn.eventId)));
  const older = indexed
    .filter((turn) => !loadedIds.has(turn.event_id) && parseUTC(turn.timestamp).getTime() <= loadedFromMs)
    .map(turnFromIndex);
  if (older.length === 0) return loaded;
  // A page can start on the image or text block of a prompt whose first block
  // is just outside it; that block is part of the indexed turn, not a new one.
  const olderIds = older.map((turn) => String(turn.eventId));
  const inWindow = loaded.filter(
    (turn) => !olderIds.some((base) => isPromptFragmentOf(base, String(turn.eventId))),
  );
  return [...older, ...inWindow];
}

function TurnList({
  turns,
  runningTurnKey,
  currentTurnKey,
  onSelectTurn,
}: {
  turns: TurnOutlineTurn[];
  runningTurnKey: string | null;
  currentTurnKey: string | null;
  onSelectTurn: (turn: TurnOutlineTurn) => void;
}) {
  const listRef = useRef<HTMLOListElement | null>(null);

  // Keep the current row inside the list's scroll viewport as the transcript
  // scrolls or when the popover opens.
  useEffect(() => {
    const list = listRef.current;
    if (!list) return;
    const reveal = () => {
      const row = list.querySelector<HTMLElement>('[aria-current="true"]');
      if (!row) return;
      const listBox = list.getBoundingClientRect();
      const rowBox = row.getBoundingClientRect();
      if (rowBox.top < listBox.top) list.scrollTop -= listBox.top - rowBox.top;
      else if (rowBox.bottom > listBox.bottom) list.scrollTop += rowBox.bottom - listBox.bottom;
    };
    reveal();
    if (typeof ResizeObserver === "undefined") return;
    const resizes = new ResizeObserver(reveal);
    resizes.observe(list);
    return () => resizes.disconnect();
  }, [currentTurnKey, turns.length]);

  return (
    <ol ref={listRef} className="session-turn-outline__list">
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
  );
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
  /** `ticks` (default): quiet right-edge tick strip with hover/click popover.
   * `pinned`: docked right-hand turns drawer. */
  mode?: "ticks" | "pinned";
  /** Whether the Pin button is shown in the hover card (disabled on narrow
   * viewports where a docked right column would squeeze the transcript). */
  canPin?: boolean;
  onPinnedChange?: (pinned: boolean) => void;
  /** Transcript scroll container for seamless wheel passthrough over the tick strip. */
  scrollContainer?: HTMLElement | null;
}

type CardOpenSource = null | "hover" | "click";

export function TurnOutline({
  turns,
  runningTurnKey = null,
  currentTurnKey = null,
  onSelectTurn,
  mode = "ticks",
  canPin = true,
  onPinnedChange,
  scrollContainer = null,
}: TurnOutlineProps) {
  const [openSource, setOpenSource] = useState<CardOpenSource>(null);
  const openTimerRef = useRef<number | null>(null);
  const closeTimerRef = useRef<number | null>(null);
  const layerRef = useRef<HTMLDivElement | null>(null);
  const railRef = useRef<HTMLButtonElement | null>(null);

  const cardOpen = openSource !== null;

  const clearTimers = useCallback(() => {
    if (openTimerRef.current !== null) {
      window.clearTimeout(openTimerRef.current);
      openTimerRef.current = null;
    }
    if (closeTimerRef.current !== null) {
      window.clearTimeout(closeTimerRef.current);
      closeTimerRef.current = null;
    }
  }, []);

  const closeCard = useCallback(() => {
    clearTimers();
    setOpenSource(null);
  }, [clearTimers]);

  const scheduleHoverOpen = useCallback(() => {
    if (closeTimerRef.current !== null) {
      window.clearTimeout(closeTimerRef.current);
      closeTimerRef.current = null;
    }
    if (openTimerRef.current !== null) return;
    openTimerRef.current = window.setTimeout(() => {
      openTimerRef.current = null;
      setOpenSource((current) => current ?? "hover");
    }, HOVER_OPEN_DELAY_MS);
  }, []);

  const scheduleHoverClose = useCallback(() => {
    if (openTimerRef.current !== null) {
      window.clearTimeout(openTimerRef.current);
      openTimerRef.current = null;
    }
    if (closeTimerRef.current !== null) {
      window.clearTimeout(closeTimerRef.current);
    }
    closeTimerRef.current = window.setTimeout(() => {
      closeTimerRef.current = null;
      setOpenSource((current) => (current === "hover" ? null : current));
    }, HOVER_CLOSE_DELAY_MS);
  }, []);

  useEffect(() => () => clearTimers(), [clearTimers]);

  // Reset popover when switching into pinned mode or dropping below 2 turns.
  useEffect(() => {
    if (mode === "pinned" || turns.length < 2) {
      closeCard();
    }
  }, [mode, turns.length, closeCard]);

  // Escape (contained in capture phase so it does not also trigger composer or
  // other page-level Escape handlers) and outside pointer clicks dismiss the card.
  useEffect(() => {
    if (!cardOpen || mode !== "ticks") return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopPropagation();
      closeCard();
    };
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (target instanceof Node && layerRef.current?.contains(target)) return;
      closeCard();
    };
    window.addEventListener("keydown", onKeyDown, true);
    document.addEventListener("pointerdown", onPointerDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown, true);
      document.removeEventListener("pointerdown", onPointerDown);
    };
  }, [cardOpen, mode, closeCard]);

  // Forward wheel events over the tick strip to the transcript scroller with
  // normalized deltaMode and preventDefault so the outer page cannot scroll.
  useEffect(() => {
    const rail = railRef.current;
    if (!rail || !scrollContainer || mode !== "ticks" || turns.length < 2) return;
    const onWheel = (event: WheelEvent) => {
      const scale =
        event.deltaMode === 1
          ? WHEEL_LINE_PX
          : event.deltaMode === 2
            ? scrollContainer.clientHeight || 400
            : 1;
      const top = event.deltaY * scale;
      const left = event.deltaX * scale;
      if (top === 0 && left === 0) return;
      event.preventDefault();
      if (typeof scrollContainer.scrollBy === "function") {
        scrollContainer.scrollBy({ top, left });
      } else {
        scrollContainer.scrollTop += top;
        scrollContainer.scrollLeft += left;
      }
    };
    rail.addEventListener("wheel", onWheel, { passive: false });
    return () => rail.removeEventListener("wheel", onWheel);
  }, [scrollContainer, mode, turns.length]);

  const activeIndex = useMemo(() => {
    if (!currentTurnKey) return turns.length - 1;
    const found = turns.findIndex((turn) => turn.key === currentTurnKey);
    return found >= 0 ? found : turns.length - 1;
  }, [turns, currentTurnKey]);

  const runningIndex = useMemo(
    () => (runningTurnKey ? turns.findIndex((turn) => turn.key === runningTurnKey) : -1),
    [turns, runningTurnKey],
  );

  const ticks = useMemo(
    () => buildTickMarks(turns.length, activeIndex, runningIndex),
    [turns.length, activeIndex, runningIndex],
  );

  // A 0- or 1-turn session has nowhere to jump: stay out of the way.
  if (turns.length < 2) return null;

  const headingLabel = `${turns.length} turns`;

  if (mode === "pinned") {
    return (
      <aside
        className="session-turn-drawer"
        data-testid="session-turn-outline"
        aria-label="Session turns"
      >
        <div className="session-turn-outline__head">
          <span className="session-turn-outline__heading">{headingLabel}</span>
          {onPinnedChange ? (
            <button
              type="button"
              className="session-turn-outline__pin-btn"
              data-testid="turn-outline-unpin"
              onClick={() => onPinnedChange(false)}
              title="Collapse to right-edge ticks"
            >
              Unpin
            </button>
          ) : null}
        </div>
        <TurnList
          turns={turns}
          runningTurnKey={runningTurnKey}
          currentTurnKey={currentTurnKey}
          onSelectTurn={onSelectTurn}
        />
      </aside>
    );
  }

  return (
    <div ref={layerRef} className="turn-ticks-layer" data-testid="turn-ticks-layer">
      <button
        ref={railRef}
        type="button"
        className={`turn-ticks__rail${cardOpen ? " is-open" : ""}`}
        data-testid="turn-ticks-trigger"
        aria-label={`Session turns (${activeIndex + 1} of ${turns.length})`}
        aria-haspopup="dialog"
        aria-expanded={cardOpen}
        onMouseEnter={scheduleHoverOpen}
        onMouseLeave={scheduleHoverClose}
        onClick={() => {
          clearTimers();
          // Hovering then clicking promotes the hover preview to click-open
          // instead of flickering it shut; a second click closes it.
          setOpenSource((current) => (current === "click" ? null : "click"));
        }}
        title={`${headingLabel} · hover or click to jump`}
      >
        {ticks.map((tick) => (
          <span
            key={tick.index}
            className={`turn-ticks__mark${tick.isCurrent ? " is-current" : tick.isPast ? " is-past" : ""}${tick.isRunning ? " is-running" : ""}`}
            aria-hidden="true"
          />
        ))}
      </button>

      {cardOpen ? (
        <nav
          className="turn-ticks__card"
          role="dialog"
          aria-label="Session turns"
          data-testid="session-turn-outline"
          onMouseEnter={clearTimers}
          onMouseLeave={scheduleHoverClose}
        >
          <div className="session-turn-outline__head">
            <span className="session-turn-outline__heading">{headingLabel}</span>
            {canPin && onPinnedChange ? (
              <button
                type="button"
                className="session-turn-outline__pin-btn"
                data-testid="turn-outline-pin"
                onClick={() => {
                  closeCard();
                  onPinnedChange(true);
                }}
                title="Pin turns panel on the right"
              >
                Pin
              </button>
            ) : null}
          </div>
          <TurnList
            turns={turns}
            runningTurnKey={runningTurnKey}
            currentTurnKey={currentTurnKey}
            onSelectTurn={(turn) => {
              onSelectTurn(turn);
              closeCard();
            }}
          />
        </nav>
      ) : null}
    </div>
  );
}
