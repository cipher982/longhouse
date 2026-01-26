import { useEffect, useMemo, useReducer, useRef, useState } from "react";
import { advanceForumReplay, createForumReplayCursor, type ForumReplayCursor } from "./replay";
import type { ForumReplayScenario, ForumReplayEvent } from "./types";
import { applyForumEvent, type ForumMapState } from "./state";

export type ForumReplayPlayerOptions = {
  loop?: boolean;
  speed?: number;
  playing?: boolean;
};

export type ForumReplayPlayer = {
  state: ForumMapState;
  timeMs: number;
  durationMs: number;
  playing: boolean;
  /** Version counter incremented on each state mutation for stable memoization */
  stateVersion: number;
  setPlaying: (next: boolean) => void;
  reset: () => void;
  dispatchEvent: (event: ForumReplayEvent) => void;
  dispatchEvents: (events: ForumReplayEvent[]) => void;
};

export function useForumReplayPlayer(
  scenario: ForumReplayScenario,
  options: ForumReplayPlayerOptions = {},
): ForumReplayPlayer {
  const { loop = true, speed = 1, playing: defaultPlaying = true } = options;
  const [playing, setPlaying] = useState(defaultPlaying);
  const [, forceRender] = useReducer((x) => x + 1, 0);

  const cursorRef = useRef<ForumReplayCursor>(createForumReplayCursor(scenario));
  const stateRef = useRef<ForumMapState>(cursorRef.current.state);
  const timeRef = useRef<number>(0);
  const rafRef = useRef<number | null>(null);
  const lastFrameRef = useRef<number | null>(null);
  const versionRef = useRef<number>(0);

  const reset = useMemo(() => {
    return () => {
      cursorRef.current = createForumReplayCursor(scenario);
      stateRef.current = cursorRef.current.state;
      timeRef.current = 0;
      lastFrameRef.current = null;
      versionRef.current += 1;
      forceRender();
    };
  }, [scenario]);

  const dispatchEvent = useMemo(() => {
    return (event: ForumReplayEvent) => {
      applyForumEvent(cursorRef.current.state, event);
      stateRef.current = cursorRef.current.state;
      const now = cursorRef.current.state.now;
      cursorRef.current.now = Math.max(cursorRef.current.now, now);
      timeRef.current = now;
      versionRef.current += 1;
      forceRender();
    };
  }, []);

  const dispatchEvents = useMemo(() => {
    return (events: ForumReplayEvent[]) => {
      events.forEach((event) => applyForumEvent(cursorRef.current.state, event));
      stateRef.current = cursorRef.current.state;
      const now = cursorRef.current.state.now;
      cursorRef.current.now = Math.max(cursorRef.current.now, now);
      timeRef.current = now;
      versionRef.current += 1;
      forceRender();
    };
  }, []);

  useEffect(() => {
    reset();
  }, [reset]);

  useEffect(() => {
    if (!playing) {
      if (rafRef.current != null) {
        cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }
      lastFrameRef.current = null;
      return;
    }

    const tick = (now: number) => {
      if (lastFrameRef.current == null) {
        lastFrameRef.current = now;
      }
      const delta = (now - lastFrameRef.current) * speed;
      lastFrameRef.current = now;

      const cursor = cursorRef.current;
      let targetTime = cursor.now + delta;

      if (targetTime >= scenario.durationMs) {
        if (loop) {
          const overflow = scenario.durationMs > 0 ? targetTime % scenario.durationMs : 0;
          cursorRef.current = createForumReplayCursor(scenario);
          stateRef.current = cursorRef.current.state;
          timeRef.current = 0;
          lastFrameRef.current = now;
          targetTime = overflow;
        } else {
          setPlaying(false);
          return;
        }
      }

      const applied = advanceForumReplay(cursorRef.current, targetTime);
      stateRef.current = cursorRef.current.state;
      timeRef.current = cursorRef.current.now;
      // Always render on each tick so marker expiry and time display stay in sync
      // even when no new events are applied
      if (applied > 0) {
        versionRef.current += 1;
      }
      forceRender();
      rafRef.current = requestAnimationFrame(tick);
    };

    rafRef.current = requestAnimationFrame(tick);

    return () => {
      if (rafRef.current != null) {
        cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }
    };
  }, [loop, playing, scenario, speed]);

  return {
    state: stateRef.current,
    timeMs: timeRef.current,
    durationMs: scenario.durationMs,
    playing,
    stateVersion: versionRef.current,
    setPlaying,
    reset,
    dispatchEvent,
    dispatchEvents,
  };
}
