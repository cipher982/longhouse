import { useEffect, useMemo, useReducer, useRef, useState } from "react";
import { advanceSwarmReplay, createSwarmReplayCursor, type SwarmReplayCursor } from "./replay";
import type { SwarmReplayScenario, SwarmReplayEvent } from "./types";
import { applySwarmEvent, type SwarmMapState } from "./state";

export type SwarmReplayPlayerOptions = {
  loop?: boolean;
  speed?: number;
  playing?: boolean;
};

export type SwarmReplayPlayer = {
  state: SwarmMapState;
  timeMs: number;
  durationMs: number;
  playing: boolean;
  setPlaying: (next: boolean) => void;
  reset: () => void;
  dispatchEvent: (event: SwarmReplayEvent) => void;
  dispatchEvents: (events: SwarmReplayEvent[]) => void;
};

export function useSwarmReplayPlayer(
  scenario: SwarmReplayScenario,
  options: SwarmReplayPlayerOptions = {},
): SwarmReplayPlayer {
  const { loop = true, speed = 1, playing: defaultPlaying = true } = options;
  const [playing, setPlaying] = useState(defaultPlaying);
  const [, forceRender] = useReducer((x) => x + 1, 0);

  const cursorRef = useRef<SwarmReplayCursor>(createSwarmReplayCursor(scenario));
  const stateRef = useRef<SwarmMapState>(cursorRef.current.state);
  const timeRef = useRef<number>(0);
  const rafRef = useRef<number | null>(null);
  const lastFrameRef = useRef<number | null>(null);

  const reset = useMemo(() => {
    return () => {
      cursorRef.current = createSwarmReplayCursor(scenario);
      stateRef.current = cursorRef.current.state;
      timeRef.current = 0;
      lastFrameRef.current = null;
      forceRender();
    };
  }, [scenario]);

  const dispatchEvent = useMemo(() => {
    return (event: SwarmReplayEvent) => {
      applySwarmEvent(cursorRef.current.state, event);
      stateRef.current = cursorRef.current.state;
      const now = cursorRef.current.state.now;
      cursorRef.current.now = Math.max(cursorRef.current.now, now);
      timeRef.current = now;
      forceRender();
    };
  }, []);

  const dispatchEvents = useMemo(() => {
    return (events: SwarmReplayEvent[]) => {
      events.forEach((event) => applySwarmEvent(cursorRef.current.state, event));
      stateRef.current = cursorRef.current.state;
      const now = cursorRef.current.state.now;
      cursorRef.current.now = Math.max(cursorRef.current.now, now);
      timeRef.current = now;
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
          cursorRef.current = createSwarmReplayCursor(scenario);
          stateRef.current = cursorRef.current.state;
          timeRef.current = 0;
          lastFrameRef.current = now;
          targetTime = overflow;
        } else {
          setPlaying(false);
          return;
        }
      }

      const applied = advanceSwarmReplay(cursorRef.current, targetTime);
      stateRef.current = cursorRef.current.state;
      timeRef.current = cursorRef.current.now;
      if (applied > 0) {
        forceRender();
      }
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
    setPlaying,
    reset,
    dispatchEvent,
    dispatchEvents,
  };
}
