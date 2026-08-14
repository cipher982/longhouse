import { readFileSync } from "node:fs";
import path from "node:path";
import { brotliCompressSync, gzipSync } from "node:zlib";
import { describe, expect, it } from "vitest";
import { REMOTE_SCENE_DATA } from "./generated/sceneData";
import { selectGlyphIndex } from "./glyphRenderer";
import { projectPoint, type CameraFrame } from "./sceneMath";
import { clampFrameIndex, decodeFrame, decodeFrames, encodeFrame } from "./sceneCodec";
import {
  getRemoteSceneWorkState,
  getRemoteScenePlaybackPosition,
  normalizeRemoteSceneFrame,
  REMOTE_SCENE_LOOP_START_FRAME,
  REMOTE_SCENE_PLAYBACK_FRAME_COUNT,
  REMOTE_SCENE_PLAYBACK_LAST_FRAME,
} from "./recordedTimeline";
import { SCENE_GLYPHS, SCENE_SPEC } from "./sceneSpec";
import { createSourceRaster, drawTriangle3D } from "./sourceRenderer";

describe("remote scene codec", () => {
  const encodedStreams = readFileSync(
    path.resolve(process.cwd(), "src/components/remote-scene/generated/sceneFrames.txt"),
    "utf8",
  ).trimEnd().split("\n");

  it("round-trips palette values through inspectable RLE", () => {
    const source = Uint8Array.from([0, 0, 3, 3, 3, 18, 0, 32]);
    expect(decodeFrame(encodeFrame(source), source.length)).toEqual(source);
  });

  it("decodes every generated profile to its declared dimensions", () => {
    for (const profile of Object.values(REMOTE_SCENE_DATA.profiles)) {
      const frames = decodeFrames(encodedStreams[profile.streamIndex], profile.width * profile.height);
      expect(frames).toHaveLength(REMOTE_SCENE_DATA.fps * REMOTE_SCENE_DATA.durationSeconds);
      expect(frames[0]).toHaveLength(profile.width * profile.height);
      expect(frames.at(-1)).toHaveLength(profile.width * profile.height);
    }
    expect(REMOTE_SCENE_DATA.palette.length).toBeLessThanOrEqual(35);
  });

  it("keeps the generated runtime module within its compressed delivery budget", () => {
    const moduleBytes = readFileSync(path.resolve(process.cwd(), "src/components/remote-scene/generated/sceneData.ts"));
    const frameBytes = readFileSync(path.resolve(process.cwd(), "src/components/remote-scene/generated/sceneFrames.txt"));
    const runtimeBytes = Buffer.concat([moduleBytes, frameBytes]);
    expect(gzipSync(runtimeBytes, { level: 9 }).byteLength).toBeLessThanOrEqual(80 * 1024);
    expect(brotliCompressSync(runtimeBytes).byteLength).toBeLessThanOrEqual(55 * 1024);
  });

  it("clamps stale player state to the available generated frames", () => {
    expect(clampFrameIndex(143, 144)).toBe(143);
    expect(clampFrameIndex(199, 144)).toBe(143);
    expect(clampFrameIndex(-4, 144)).toBe(0);
    expect(clampFrameIndex(Number.NaN, 144)).toBe(0);
  });
});

describe("cinematic source projection", () => {
  const camera: CameraFrame = {
    position: [0, 0, 10],
    target: [0, 0, 0],
    up: [0, 1, 0],
    verticalFovDegrees: 50,
  };

  it("projects the camera target into the raster center", () => {
    const projected = projectPoint([0, 0, 0], camera, 200, 100);
    expect(projected.visible).toBe(true);
    expect(projected.x).toBeCloseTo(100);
    expect(projected.y).toBeCloseTo(50);
  });

  it("keeps the nearest triangle in the depth buffer", () => {
    const raster = createSourceRaster(40, 30);
    drawTriangle3D(raster, camera, [-3, -2, 0], [3, -2, 0], [0, 3, 0], { material: 1, brightness: 0.5 });
    drawTriangle3D(raster, camera, [-2, -1, 3], [2, -1, 3], [0, 2, 3], { material: 4, brightness: 1 });
    const center = 15 * raster.width + 20;
    expect(raster.material[center]).toBe(4);
    expect(raster.depth[center]).toBeLessThan(10);
  });
});

describe("terminal story synchronization", () => {
  it("keeps the recorded opening synchronized, then advances through simulated follow-up tasks", () => {
    const openingHold = getRemoteSceneWorkState(0);
    const openingAction = getRemoteSceneWorkState(33);
    const openingComplete = getRemoteSceneWorkState(143);
    const followUpStarted = getRemoteSceneWorkState(REMOTE_SCENE_LOOP_START_FRAME);
    const followUpWorking = getRemoteSceneWorkState(REMOTE_SCENE_LOOP_START_FRAME + 13);
    const followUpComplete = getRemoteSceneWorkState(REMOTE_SCENE_LOOP_START_FRAME + 126);
    const nextTask = getRemoteSceneWorkState(REMOTE_SCENE_LOOP_START_FRAME + 144);

    expect(getRemoteSceneWorkState(32).replaySecond).toBe(openingHold.replaySecond);
    expect(openingAction.replaySecond).toBeGreaterThan(openingHold.replaySecond);
    expect(getRemoteSceneWorkState(128).replaySecond).toBe(openingComplete.replaySecond);
    expect(followUpStarted.phase).toBe("working");
    expect(followUpStarted.timeline.meta.rows).toBe(14);
    expect(followUpWorking.phase).toBe("working");
    expect(followUpWorking.replaySecond).toBeGreaterThan(followUpStarted.replaySecond);
    expect(followUpComplete.phase).toBe("complete");
    expect(nextTask.id).not.toBe(followUpStarted.id);
    expect(nextTask.prompt).not.toBe(followUpStarted.prompt);
  });

  it("loops work without replaying the departure scene", () => {
    expect(REMOTE_SCENE_PLAYBACK_FRAME_COUNT).toBe(432);
    expect(normalizeRemoteSceneFrame(REMOTE_SCENE_PLAYBACK_LAST_FRAME)).toBe(431);
    expect(normalizeRemoteSceneFrame(REMOTE_SCENE_PLAYBACK_FRAME_COUNT)).toBe(REMOTE_SCENE_LOOP_START_FRAME);
    expect(normalizeRemoteSceneFrame(REMOTE_SCENE_PLAYBACK_FRAME_COUNT + 287)).toBe(431);
    expect(getRemoteScenePlaybackPosition(REMOTE_SCENE_PLAYBACK_FRAME_COUNT)).toEqual({
      frameIndex: REMOTE_SCENE_LOOP_START_FRAME,
      workCycle: 1,
    });
    expect(getRemoteSceneWorkState(REMOTE_SCENE_LOOP_START_FRAME, "qa", 1).id)
      .not.toBe(getRemoteSceneWorkState(REMOTE_SCENE_LOOP_START_FRAME, "qa", 0).id);
  });
});

describe("shape-aware glyph selection", () => {
  it("uses directional glyphs for canonical horizontal and vertical edges", () => {
    const horizontal = selectGlyphIndex([0, 0, 0.7, 0.7, 0, 0], { x: 0, y: 0.8 }, undefined, false);
    const vertical = selectGlyphIndex([0.25, 0.25, 0.72, 0.72, 0.25, 0.25], { x: 0.8, y: 0 }, undefined, false);
    expect(SCENE_GLYPHS[horizontal]).toBe("-");
    expect(SCENE_GLYPHS[vertical]).toBe("|");
  });

  it("retains a near-equivalent previous glyph but releases on a strong change", () => {
    const retained = selectGlyphIndex([0, 0, 0.65, 0.65, 0, 0], { x: 0, y: 0 }, 1, true);
    const released = selectGlyphIndex([0.9, 0.9, 0.94, 0.94, 0.9, 0.9], { x: 0, y: 0 }, 1, true);
    expect(retained).toBe(1);
    expect(SCENE_GLYPHS[released]).toBe("@");
  });

  it("keeps profile sample dimensions aligned with the six-value atlas", () => {
    expect(SCENE_SPEC.samplesPerCell).toEqual({ x: 2, y: 3 });
  });
});
