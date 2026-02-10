import { interpolate } from "remotion";

export const FPS = 30;
export const WIDTH = 1920;
export const HEIGHT = 1080;

/** Convert seconds to frames */
export const sec = (s: number) => Math.round(s * FPS);

/** Transition duration between scenes (frames) */
export const TRANSITION_FRAMES = 15; // 0.5s

/** Audio durations from TTS (seconds) */
export const AUDIO_DURATIONS = {
  hook: 4.848,
  searchWow: 2.568,
  timelineExplore: 10.056,
  sessionDetail: 7.008,
  cta: 6.216,
} as const;

/** Raw video clip durations (seconds) — from Playwright captures */
export const CLIP_DURATIONS = {
  hook: 6.13, // programmatic, no clip, but keep for scene length
  searchWow: 4.93,
  timelineExplore: 10.83,
  sessionDetail: 7.8,
  cta: 8.0,
} as const;

/** Scene durations in frames (based on clip lengths) */
export const SCENE_FRAMES = {
  hook: sec(CLIP_DURATIONS.hook),
  searchWow: sec(CLIP_DURATIONS.searchWow),
  timelineExplore: sec(CLIP_DURATIONS.timelineExplore),
  sessionDetail: sec(CLIP_DURATIONS.sessionDetail),
  cta: sec(CLIP_DURATIONS.cta),
} as const;

const SCENE_ORDER = [
  "hook",
  "searchWow",
  "timelineExplore",
  "sessionDetail",
  "cta",
] as const;

/** Compute scene start frames accounting for transition overlaps */
export function computeSceneStarts(): Record<string, number> {
  const starts: Record<string, number> = {};
  let offset = 0;
  for (const scene of SCENE_ORDER) {
    starts[scene] = offset;
    offset += SCENE_FRAMES[scene] - TRANSITION_FRAMES;
  }
  return starts;
}

export const SCENE_STARTS = computeSceneStarts();

/** Total composition duration in frames */
export const TOTAL_FRAMES =
  Object.values(SCENE_FRAMES).reduce((a, b) => a + b, 0) -
  (SCENE_ORDER.length - 1) * TRANSITION_FRAMES;
