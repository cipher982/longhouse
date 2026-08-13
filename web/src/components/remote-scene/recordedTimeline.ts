import {
  claudeAddtest,
  claudeSteer,
  claudeTile,
  recordingPrompt,
  steerWindow,
  type GridTimeline,
} from "@longhouse/video/demo";

const SCENE_FPS = 24;
const INTRO_LAST_FRAME = 143;
const INTRO_REPLAY_START_FRAME = 32;
const INTRO_REPLAY_END_FRAME = 128;
const LOOP_TASK_LEAD_FRAMES = 12;
const LOOP_TASK_COMPLETE_FRAME = 126;

export const REMOTE_SCENE_LOOP_START_FRAME = INTRO_LAST_FRAME + 1;
export const REMOTE_SCENE_TASK_FRAMES = 6 * SCENE_FPS;

function bottomViewport(timeline: GridTimeline, rows: number): GridTimeline {
  if (timeline.meta.rows <= rows) return timeline;
  const firstRow = timeline.meta.rows - rows;
  return {
    ...timeline,
    meta: { ...timeline.meta, rows },
    states: timeline.states.map((state) => ({
      ...state,
      rows: state.rows.slice(firstRow),
      cursor: {
        ...state.cursor,
        y: Math.max(0, state.cursor.y - firstRow),
        visible: state.cursor.visible && state.cursor.y >= firstRow,
      },
    })),
  };
}

const loopTasks = [
  {
    id: "add-empty-shelf-test",
    timeline: bottomViewport(claudeAddtest, 14),
    window: steerWindow(claudeAddtest, 8.791),
  },
  {
    id: "fix-inventory-count",
    timeline: bottomViewport(claudeSteer, 14),
    window: steerWindow(claudeSteer, 8.493),
  },
] as const;

export const REMOTE_SCENE_LOOP_FRAME_COUNT = loopTasks.length * REMOTE_SCENE_TASK_FRAMES;
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
}

function lerpWindow(startSec: number, endSec: number, progress: number): number {
  return startSec + (endSec - startSec) * Math.max(0, Math.min(1, progress));
}

export function normalizeRemoteSceneFrame(frameIndex: number): number {
  const wholeFrame = Math.max(0, Math.floor(frameIndex));
  if (wholeFrame < REMOTE_SCENE_PLAYBACK_FRAME_COUNT) return wholeFrame;
  return REMOTE_SCENE_LOOP_START_FRAME
    + ((wholeFrame - REMOTE_SCENE_LOOP_START_FRAME) % REMOTE_SCENE_LOOP_FRAME_COUNT);
}

export function getRemoteSceneWorkState(frameIndex: number): RemoteSceneWorkState {
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
    };
  }

  const loopFrame = normalizedFrame - REMOTE_SCENE_LOOP_START_FRAME;
  const taskIndex = Math.floor(loopFrame / REMOTE_SCENE_TASK_FRAMES);
  const taskFrame = loopFrame % REMOTE_SCENE_TASK_FRAMES;
  const task = loopTasks[taskIndex];
  const replayProgress = (taskFrame - LOOP_TASK_LEAD_FRAMES)
    / (LOOP_TASK_COMPLETE_FRAME - LOOP_TASK_LEAD_FRAMES);
  const phase: RemoteSceneWorkPhase = taskFrame < LOOP_TASK_LEAD_FRAMES
    ? "queued"
    : taskFrame < LOOP_TASK_COMPLETE_FRAME
      ? "working"
      : "complete";

  return {
    id: task.id,
    timeline: task.timeline,
    replaySecond: phase === "queued"
      ? task.window.startSec
      : lerpWindow(task.window.startSec, task.window.endSec, replayProgress),
    prompt: recordingPrompt(task.timeline),
    phase,
    progress: Math.max(0, Math.min(1, replayProgress)),
    taskNumber: taskIndex + 2,
  };
}

export function remoteSceneLoopProgress(frameIndex: number): number {
  if (frameIndex < REMOTE_SCENE_LOOP_START_FRAME) return 0;
  return (normalizeRemoteSceneFrame(frameIndex) - REMOTE_SCENE_LOOP_START_FRAME)
    / REMOTE_SCENE_LOOP_FRAME_COUNT;
}
