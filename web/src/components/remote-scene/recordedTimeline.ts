import { claudeTile, steerWindow } from "@longhouse/video/demo";

export const REMOTE_SCENE_RECORDING = claudeTile;
export const REMOTE_SCENE_REPLAY_WINDOW = steerWindow(claudeTile, 8.6);

const REPLAY_START_FRAME = 32;
const REPLAY_END_FRAME = 128;

export function replaySecondForSceneFrame(frameIndex: number): number {
  if (frameIndex <= REPLAY_START_FRAME) return REMOTE_SCENE_REPLAY_WINDOW.holdSec;
  const progress = Math.max(
    0,
    Math.min(1, (frameIndex - REPLAY_START_FRAME) / (REPLAY_END_FRAME - REPLAY_START_FRAME)),
  );
  return REMOTE_SCENE_REPLAY_WINDOW.startSec
    + (REMOTE_SCENE_REPLAY_WINDOW.endSec - REMOTE_SCENE_REPLAY_WINDOW.startSec) * progress;
}
