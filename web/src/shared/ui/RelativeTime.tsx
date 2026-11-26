import { useState, useSyncExternalStore } from "react";
import { parseUTC } from "@/shared/lib/dateUtils";

/**
 * "4 min ago" for recent times, a clock time once it is older than a day.
 *
 * A reader cares how stale a message is, not the minute it arrived: a clock
 * time makes them look at a clock and subtract. The exact time is one hover
 * (title) or one tap (toggles the label) away for matching against logs.
 */

const TICK_MS = 30_000;
const listeners = new Set<() => void>();
let nowMs = Date.now();
let timer: ReturnType<typeof setInterval> | null = null;

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  if (timer === null) {
    nowMs = Date.now();
    timer = setInterval(() => {
      nowMs = Date.now();
      for (const fn of listeners) fn();
    }, TICK_MS);
  }
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0 && timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  };
}

function getNow(): number {
  return nowMs;
}

/** The shared clock every relative label reads, advanced every 30 s. */
export function useNowTick(): number {
  return useSyncExternalStore(subscribe, getNow, getNow);
}

const DAY_MS = 86_400_000;

function clockTime(date: Date): string {
  return date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

function sameLocalDay(a: Date, b: Date): boolean {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}

/** Relative label for `date` as seen at `now`. Exported for tests. */
export function formatAgo(date: Date, now: number): string {
  const diff = now - date.getTime();
  if (diff < 60_000) return "just now";
  const mins = Math.floor(diff / 60_000);
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.floor(diff / 3_600_000);
  if (diff < DAY_MS) return `${hours} h ago`;
  const yesterday = new Date(now - DAY_MS);
  if (sameLocalDay(date, yesterday)) return `Yesterday ${clockTime(date)}`;
  const sameYear = date.getFullYear() === new Date(now).getFullYear();
  const day = date.toLocaleDateString(undefined, sameYear
    ? { month: "short", day: "numeric" }
    : { month: "short", day: "numeric", year: "numeric" });
  return `${day}, ${clockTime(date)}`;
}

/** Full timestamp for the tooltip and the tapped state. */
export function formatExact(date: Date): string {
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function RelativeTime({
  iso,
  className,
  testId,
}: {
  iso: string | null | undefined;
  className?: string;
  testId?: string;
}) {
  const now = useNowTick();
  const [exact, setExact] = useState(false);
  if (!iso) return null;
  const date = parseUTC(iso);
  if (Number.isNaN(date.getTime())) return null;
  const full = formatExact(date);
  return (
    <time
      dateTime={date.toISOString()}
      title={full}
      className={className ? `lh-reltime ${className}` : "lh-reltime"}
      data-testid={testId}
      onClick={(e) => {
        e.stopPropagation();
        setExact((v) => !v);
      }}
    >
      {exact ? full : formatAgo(date, Math.max(now, Date.now() - TICK_MS))}
    </time>
  );
}
