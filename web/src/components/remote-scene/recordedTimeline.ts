import {
  claudeTile,
  recordingPrompt,
  steerWindow,
  type GridTimeline,
} from "@longhouse/video/demo";
import { generateDemoStory, getDemoStoryState } from "../../lib/demoSimulation";

const SCENE_FPS = 24;
const INTRO_LAST_FRAME = 143;
const INTRO_REPLAY_START_FRAME = 32;
const INTRO_REPLAY_END_FRAME = 128;
const LOOP_TASK_LEAD_FRAMES = 0;
const LOOP_TASK_COMPLETE_FRAME = 126;

export const REMOTE_SCENE_LOOP_START_FRAME = INTRO_LAST_FRAME + 1;
export const REMOTE_SCENE_TASK_FRAMES = 6 * SCENE_FPS;

const LOOP_TASK_COUNT = 2;

export const REMOTE_SCENE_LOOP_FRAME_COUNT = LOOP_TASK_COUNT * REMOTE_SCENE_TASK_FRAMES;
export const REMOTE_SCENE_PLAYBACK_FRAME_COUNT = REMOTE_SCENE_LOOP_START_FRAME + REMOTE_SCENE_LOOP_FRAME_COUNT;
export const REMOTE_SCENE_PLAYBACK_LAST_FRAME = REMOTE_SCENE_PLAYBACK_FRAME_COUNT - 1;

export type RemoteSceneWorkPhase = "queued" | "working" | "complete";

export interface RemoteSceneWorkState {
  id: string;
  timeline: GridTimeline;
  replaySecond: number;
  prompt: string;
  phase: RemoteSceneWorkPhase;
  progress: number;
  taskNumber: number;
  message: string;
  detail: string;
}

function lerpWindow(startSec: number, endSec: number, progress: number): number {
  return startSec + (endSec - startSec) * Math.max(0, Math.min(1, progress));
}

export function normalizeRemoteSceneFrame(frameIndex: number): number {
  return getRemoteScenePlaybackPosition(frameIndex).frameIndex;
}

export interface RemoteScenePlaybackPosition {
  frameIndex: number;
  workCycle: number;
}

export function getRemoteScenePlaybackPosition(frameIndex: number): RemoteScenePlaybackPosition {
  const wholeFrame = Math.max(0, Math.floor(frameIndex));
  if (wholeFrame < REMOTE_SCENE_PLAYBACK_FRAME_COUNT) {
    return { frameIndex: wholeFrame, workCycle: 0 };
  }
  const loopDistance = wholeFrame - REMOTE_SCENE_LOOP_START_FRAME;
  return {
    frameIndex: REMOTE_SCENE_LOOP_START_FRAME + (loopDistance % REMOTE_SCENE_LOOP_FRAME_COUNT),
    workCycle: Math.floor(loopDistance / REMOTE_SCENE_LOOP_FRAME_COUNT),
  };
}

export function getRemoteSceneWorkState(
  frameIndex: number,
  seed = "longhouse-remote-scene",
  workCycle = 0,
): RemoteSceneWorkState {
  const normalizedFrame = normalizeRemoteSceneFrame(frameIndex);
  if (normalizedFrame <= INTRO_LAST_FRAME) {
    const window = steerWindow(claudeTile, 8.6);
    const progress = Math.max(
      0,
      Math.min(1, (normalizedFrame - INTRO_REPLAY_START_FRAME) / (INTRO_REPLAY_END_FRAME - INTRO_REPLAY_START_FRAME)),
    );
    return {
      id: "fix-inventory-count-opening",
      timeline: claudeTile,
      replaySecond: normalizedFrame <= INTRO_REPLAY_START_FRAME
        ? window.holdSec
        : lerpWindow(window.startSec, window.endSec, progress),
      prompt: recordingPrompt(claudeTile),
      phase: normalizedFrame < 84 ? "working" : normalizedFrame < 124 ? "working" : "complete",
      progress: Math.max(0, Math.min(1, (normalizedFrame - 36) / (124 - 36))),
      taskNumber: 1,
      message: normalizedFrame < 124 ? "Working" : "Ready for input",
      detail: "studio-mac",
    };
  }

  const loopFrame = normalizedFrame - REMOTE_SCENE_LOOP_START_FRAME;
  const taskIndex = Math.floor(loopFrame / REMOTE_SCENE_TASK_FRAMES);
  const taskFrame = loopFrame % REMOTE_SCENE_TASK_FRAMES;
  const story = generateDemoStory(seed, workCycle * LOOP_TASK_COUNT + taskIndex);
  const window = steerWindow(story.timeline, story.durationSec);
  const replayProgress = (taskFrame - LOOP_TASK_LEAD_FRAMES)
    / (LOOP_TASK_COMPLETE_FRAME - LOOP_TASK_LEAD_FRAMES);
  const editorialPhase: RemoteSceneWorkPhase = taskFrame < LOOP_TASK_LEAD_FRAMES
    ? "queued"
    : taskFrame < LOOP_TASK_COMPLETE_FRAME
      ? "working"
      : "complete";
  const replaySecond = editorialPhase === "queued"
    ? window.startSec
    : lerpWindow(window.startSec, window.endSec, replayProgress);
  const storyState = getDemoStoryState(story, replaySecond);
  const phase: RemoteSceneWorkPhase = editorialPhase === "queued"
    ? "queued"
    : editorialPhase === "complete" || storyState.phase === "complete"
      ? "complete"
      : "working";

  return {
    id: story.id,
    timeline: story.timeline,
    replaySecond,
    prompt: story.prompt,
    phase,
    progress: Math.max(0, Math.min(1, replayProgress)),
    taskNumber: workCycle * LOOP_TASK_COUNT + taskIndex + 2,
    message: editorialPhase === "queued" ? "Instruction received" : phase === "complete" ? "Ready for input" : storyState.message,
    detail: editorialPhase === "queued" ? story.shortLabel : storyState.detail,
  };
}

export function remoteSceneLoopProgress(frameIndex: number): number {
  if (frameIndex < REMOTE_SCENE_LOOP_START_FRAME) return 0;
  return (normalizeRemoteSceneFrame(frameIndex) - REMOTE_SCENE_LOOP_START_FRAME)
    / REMOTE_SCENE_LOOP_FRAME_COUNT;
}
