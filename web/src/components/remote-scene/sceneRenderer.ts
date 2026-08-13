import { renderGlyphFrame } from "./glyphRenderer";
import { SCENE_SPEC, type SceneProfileKey } from "./sceneSpec";
import { renderSourceScene } from "./sourceRenderer";

export function renderSceneFrame(
  profileKey: SceneProfileKey,
  frameIndex: number,
  previousFrame?: Uint8Array,
): Uint8Array {
  const frameCount = SCENE_SPEC.durationSeconds * SCENE_SPEC.fps;
  const safeFrame = Math.max(0, Math.min(frameCount - 1, frameIndex));
  return renderGlyphFrame(renderSourceScene(profileKey, safeFrame), profileKey, previousFrame);
}

export function renderSceneFrames(profileKey: SceneProfileKey): Uint8Array[] {
  const frameCount = SCENE_SPEC.durationSeconds * SCENE_SPEC.fps;
  const frames: Uint8Array[] = [];
  for (let frameIndex = 0; frameIndex < frameCount; frameIndex += 1) {
    frames.push(renderSceneFrame(profileKey, frameIndex, frames.at(-1)));
  }
  return frames;
}
