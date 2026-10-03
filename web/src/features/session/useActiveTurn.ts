/**
 * Scroll-spy for the turn outline: which turn is the reader in right now?
 *
 * A turn runs from its user message to the next one, so the active turn is the
 * last user message whose top has crossed a reference line near the top of the
 * transcript viewport. The DOM is the source of truth: user rows are queried at
 * measure time, so filters, search, pagination and live appends need no
 * bookkeeping here. Rows are in document order and therefore in `top` order,
 * which makes the lookup a binary search (log n rect reads per frame, not n).
 *
 * Measures are coalesced to one per animation frame and triggered by scroll,
 * row insertion/removal, and viewport resize — nothing polls.
 *
 * Clicking an outline row pins its turn. The pin covers the scroll the click
 * started: a smooth scroll passes through other turns on the way, and the
 * target may never reach the reference line (the last turns of a short
 * transcript cannot scroll far enough). Once that scroll settles, the pin
 * holds only while the list stays put; any later displacement (the reader
 * scrolling, a send or the "new" pill jumping to the tail) ends it.
 */
import { useCallback, useLayoutEffect, useRef, useState } from "react";

import type { AgentEventId } from "@/shared/api/agents";
import { TURN_ROW_ID_PREFIX, turnKeyForEventId, turnRowId } from "./TurnOutline";

const USER_ROW_SELECTOR = '[data-message-role="user"]';
/** The reference line sits this far down the viewport (fraction of its height). */
const REFERENCE_LINE_FRACTION = 0.2;
/** Within this many px of the end, the reader is at the bottom. */
const BOTTOM_SLOP_PX = 2;
/** The click's scroll is over after this long without a scroll event. */
const SETTLE_MS = 150;
/** Scroll offset wobble tolerated after settling (sub-pixel layout). */
const SETTLE_SLOP_PX = 1;
/** How far from the click's target the scroll may rest and still count as its own. */
const TARGET_SLOP_PX = 48;
const SCROLL_KEYS: Record<string, true> = {
  PageUp: true,
  PageDown: true,
  Home: true,
  End: true,
  ArrowUp: true,
  ArrowDown: true,
  " ": true,
};

/**
 * Index of the last item whose top is at or above `line`; 0 when every item is
 * below it (the reader is above the first turn); -1 when there are no items.
 * `topAt` must be non-decreasing in its index.
 */
export function findActiveIndex(count: number, topAt: (index: number) => number, line: number): number {
  if (count === 0) return -1;
  let low = 0;
  let high = count - 1;
  let found = 0;
  while (low <= high) {
    const mid = (low + high) >> 1;
    if (topAt(mid) <= line) {
      found = mid;
      low = mid + 1;
    } else {
      high = mid - 1;
    }
  }
  return found;
}

function measureActiveTurnKey(list: HTMLElement): string | null {
  const rows = list.querySelectorAll<HTMLElement>(USER_ROW_SELECTOR);
  if (rows.length === 0) return null;
  const scrollable = list.scrollHeight > list.clientHeight;
  const atBottom = scrollable && list.scrollHeight - list.scrollTop - list.clientHeight <= BOTTOM_SLOP_PX;
  let index = rows.length - 1;
  if (!atBottom) {
    const box = list.getBoundingClientRect();
    index = findActiveIndex(
      rows.length,
      (i) => rows[i].getBoundingClientRect().top,
      box.top + box.height * REFERENCE_LINE_FRACTION,
    );
  }
  const id = rows[index].id;
  return id.startsWith(TURN_ROW_ID_PREFIX) ? turnKeyForEventId(id.slice(TURN_ROW_ID_PREFIX.length)) : null;
}

export interface ActiveTurn {
  /** Key of the turn in view; null when the list shows no user message. */
  activeKey: string | null;
  /** Hold this turn active until the list is next moved away from where the click left it. */
  pin: (eventId: AgentEventId) => void;
}

interface Pin {
  eventId: AgentEventId;
  /** Where the list came to rest after the click's scroll; null while it travels. */
  settledTop: number | null;
  /** Where the click's smooth scroll is headed (row to the top, clamped to the end). */
  targetTop: number | null;
}

/** Whether turn `eventId`'s row overlaps the list viewport; null when it is not rendered. */
function isRowInView(list: HTMLElement, eventId: AgentEventId): boolean | null {
  const row = list.querySelector(`[id="${turnRowId(eventId)}"]`);
  if (!row) return null;
  const box = list.getBoundingClientRect();
  const rect = row.getBoundingClientRect();
  return rect.bottom > box.top && rect.top < box.bottom;
}

export function useActiveTurn(list: HTMLElement | null): ActiveTurn {
  const [activeKey, setActiveKey] = useState<string | null>(null);
  const pinnedRef = useRef<Pin | null>(null);
  const armSettleRef = useRef<() => void>(() => {});
  const listRef = useRef<HTMLElement | null>(null);

  // Layout effect: the first measure lands before paint, so the outline never
  // flashes the wrong turn when the transcript opens.
  useLayoutEffect(() => {
    listRef.current = list;
    if (!list) return;
    let frame = 0;
    let settleTimer: number | undefined;
    const measure = () => {
      frame = 0;
      const pinned = pinnedRef.current;
      if (pinned != null) {
        // A pin lives while its row exists and either the click's scroll is
        // still travelling or the row is still on screen. Anything else (a
        // filter removed it; layout shifted it away without a scroll event)
        // means the reader is somewhere else.
        const inView = isRowInView(list, pinned.eventId);
        if (inView !== null && (inView || pinned.settledTop === null)) return;
        pinnedRef.current = null;
      }
      setActiveKey(measureActiveTurnKey(list));
    };
    const schedule = () => {
      if (frame === 0) frame = requestAnimationFrame(measure);
    };
    const armSettle = () => {
      window.clearTimeout(settleTimer);
      settleTimer = window.setTimeout(() => {
        const pinned = pinnedRef.current;
        if (!pinned) return;
        // Judge whether the scroll came to rest where the click sent it; if a
        // send or the "new" pill overtook it, the pinned turn is not where we are.
        if (pinned.targetTop !== null && Math.abs(list.scrollTop - pinned.targetTop) <= TARGET_SLOP_PX) {
          pinned.settledTop = list.scrollTop;
        } else {
          pinnedRef.current = null;
          schedule();
        }
      }, SETTLE_MS);
    };
    armSettleRef.current = armSettle;
    const onScroll = () => {
      const pinned = pinnedRef.current;
      if (pinned?.settledTop === null) armSettle();
      else if (pinned && Math.abs(list.scrollTop - pinned.settledTop) > SETTLE_SLOP_PX) pinnedRef.current = null;
      schedule();
    };
    const release = () => {
      if (pinnedRef.current == null) return;
      pinnedRef.current = null;
      schedule();
    };
    // Scroll keys move the list only when focus is in it or on the page body;
    // in the composer or on a button they do something else.
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target;
      if (SCROLL_KEYS[event.key] !== true) return;
      if (target !== document.body && !(target instanceof Node && list.contains(target))) return;
      // Space on a focused button or link activates it rather than scrolling.
      if (event.key === " " && target instanceof Element && target.closest("button, a, summary, input, textarea, select, [contenteditable]")) return;
      release();
    };
    // Only the scrollbar is a pointerdown target on the list itself; a click
    // on transcript content must not cut a smooth scroll's pin short.
    const onPointerDown = (event: PointerEvent) => {
      if (event.target === list) release();
    };

    measure();
    list.addEventListener("scroll", onScroll, { passive: true });
    list.addEventListener("wheel", release, { passive: true });
    list.addEventListener("touchmove", release, { passive: true });
    list.addEventListener("pointerdown", onPointerDown, { passive: true });
    document.addEventListener("keydown", onKeyDown);
    // Height changes that add no nodes (native disclosure widgets, images)
    // shift rows without a scroll event: watch the list and its children.
    const resizes = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(schedule);
    const watched = new Set<Element>();
    resizes?.observe(list);
    const watchChildren = () => {
      if (!resizes) return;
      const children = new Set<Element>(list.children);
      for (const el of watched) {
        if (children.has(el)) continue;
        resizes.unobserve(el); // a removed wrapper must not stay referenced
        watched.delete(el);
      }
      for (const el of children) {
        if (watched.has(el)) continue;
        resizes.observe(el);
        watched.add(el);
      }
    };
    const mutations = new MutationObserver(() => {
      watchChildren();
      schedule();
    });
    mutations.observe(list, { childList: true, subtree: true });
    watchChildren();

    return () => {
      if (frame !== 0) cancelAnimationFrame(frame);
      window.clearTimeout(settleTimer);
      armSettleRef.current = () => {};
      list.removeEventListener("scroll", onScroll);
      list.removeEventListener("wheel", release);
      list.removeEventListener("touchmove", release);
      list.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
      mutations.disconnect();
      resizes?.disconnect();
    };
  }, [list]);

  const pin = useCallback((eventId: AgentEventId) => {
    const list = listRef.current;
    const row = list?.querySelector(`[id="${turnRowId(eventId)}"]`);
    let targetTop: number | null = null;
    if (list && row) {
      const wanted = list.scrollTop + row.getBoundingClientRect().top - list.getBoundingClientRect().top;
      targetTop = Math.min(Math.max(wanted, 0), Math.max(list.scrollHeight - list.clientHeight, 0));
    }
    pinnedRef.current = { eventId, settledTop: null, targetTop };
    armSettleRef.current(); // covers a click that scrolls nothing
    setActiveKey(turnKeyForEventId(eventId));
  }, []);

  return { activeKey, pin };
}
