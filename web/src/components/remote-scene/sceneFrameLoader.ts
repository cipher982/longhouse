import { REMOTE_SCENE_DATA, REMOTE_SCENE_FRAMES_URL } from "./generated/sceneData";
import { decodeFrames } from "./sceneCodec";
import type { SceneProfileKey } from "./sceneSpec";

type SceneFrames = Record<SceneProfileKey, Uint8Array[]>;

let framesPromise: Promise<SceneFrames> | null = null;

export function loadRemoteSceneFrames(): Promise<SceneFrames> {
  if (framesPromise) return framesPromise;

  framesPromise = fetch(REMOTE_SCENE_FRAMES_URL)
    .then((response) => {
      if (!response.ok) throw new Error(`remote scene frames ${response.status}`);
      return response.text();
    })
    .then((source) => {
      const streams = source.trimEnd().split("\n");
      return Object.fromEntries(
        Object.entries(REMOTE_SCENE_DATA.profiles).map(([key, profile]) => {
          const encoded = streams[profile.streamIndex];
          if (!encoded) throw new Error(`Missing remote scene stream ${profile.streamIndex}`);
          return [key, decodeFrames(encoded, profile.width * profile.height)];
        }),
      ) as SceneFrames;
    })
    .catch((error) => {
      framesPromise = null;
      throw error;
    });

  return framesPromise;
}
