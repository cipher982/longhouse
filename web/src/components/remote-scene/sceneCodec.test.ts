import { describe, expect, it } from "vitest";
import { REMOTE_SCENE_DATA } from "./generated/sceneData";
import { decodeFrame, decodeFrames, encodeFrame } from "./sceneCodec";

describe("remote scene codec", () => {
  it("round-trips a palette frame through inspectable RLE", () => {
    const source = Uint8Array.from([0, 0, 3, 3, 3, 8, 0, 9]);
    expect(decodeFrame(encodeFrame(source), source.length)).toEqual(source);
  });

  it("decodes every generated frame to the declared canvas size", () => {
    const frames = decodeFrames(
      REMOTE_SCENE_DATA.encodedFrames,
      REMOTE_SCENE_DATA.width * REMOTE_SCENE_DATA.height,
    );
    expect(frames).toHaveLength(REMOTE_SCENE_DATA.fps * REMOTE_SCENE_DATA.durationSeconds);
    expect(frames[0]).toHaveLength(REMOTE_SCENE_DATA.width * REMOTE_SCENE_DATA.height);
    expect(frames.at(-1)).toHaveLength(REMOTE_SCENE_DATA.width * REMOTE_SCENE_DATA.height);
  });
});
