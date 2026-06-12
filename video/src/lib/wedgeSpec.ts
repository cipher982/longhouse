/**
 * Typed "demo spec" for the wedge demo. An agent authors a JSON spec against
 * this shape; the SteerLoop composition is the deterministic renderer. No
 * per-video JSX is written by the agent — only data against this vocabulary.
 *
 * Kept dependency-free (plain TS types + defaultProps + `--props=spec.json`)
 * so the composition is agent-drivable without adding a schema runtime.
 */

import { FPS } from "./timing";

export type SceneKind = "hook" | "screenshot" | "steer" | "cta";

/** Fixed scene vocabulary. The agent picks a kind and fills its data. */
export interface BaseScene {
  kind: SceneKind;
  /** Seconds the scene holds on screen (before the next transition). */
  seconds: number;
  /** Caption shown over the scene (kinetic text). Optional. */
  caption?: string;
}

export interface HookScene extends BaseScene {
  kind: "hook";
  /** Big kinetic headline, e.g. the wedge in one line. */
  headline: string;
}

export interface ScreenshotScene extends BaseScene {
  kind: "screenshot";
  /** Key under video/public/shots/, e.g. "timeline-preview.png". */
  shot: string;
  /** Device frame to wrap the shot in. */
  frame: "browser" | "phone";
}

export interface SteerScene extends BaseScene {
  kind: "steer";
  /** Phone shot (the device you steer FROM). */
  phoneShot: string;
  /** The instruction the user "sends" — typed in over the scene. */
  message: string;
}

export interface CtaScene extends BaseScene {
  kind: "cta";
  headline: string;
  subhead?: string;
}

export type Scene = HookScene | ScreenshotScene | SteerScene | CtaScene;

export interface WedgeSpec {
  /** Background fill. */
  background: string;
  scenes: Scene[];
  /** Index signature so the spec satisfies Remotion's Composition props bound. */
  [key: string]: unknown;
}

/** Frames a scene occupies (its hold time; transitions overlap separately). */
export const sceneFrames = (scene: Scene): number =>
  Math.max(1, Math.round(scene.seconds * FPS));

/**
 * Default wedge spec — the launch demo. Tells the wedge in four beats using
 * the real captured assets:
 *   hook → find it (web timeline) → steer it (phone) → cta
 * Every screenshot is a real render copied into video/public/shots/.
 */
export const defaultWedgeSpec: WedgeSpec = {
  background: "#0a0a0f",
  scenes: [
    {
      kind: "hook",
      seconds: 3.5,
      headline: "Start a coding agent. Walk away.",
    },
    {
      kind: "screenshot",
      seconds: 4,
      shot: "timeline-preview.png",
      frame: "browser",
      caption: "Every session, one timeline — on machines you own.",
    },
    {
      kind: "steer",
      seconds: 5,
      phoneShot: "session-dark.png",
      message: "rebase onto main and rerun the tests",
      caption: "Steer it from your phone.",
    },
    {
      kind: "cta",
      seconds: 3.5,
      headline: "Longhouse",
      subhead: "Self-hosted · cross-provider · yours",
    },
  ],
};
