export type ScenePaletteEntry = {
  glyph: string;
  color: string;
  label: string;
};

export type SceneCamera = {
  scale: number;
  lateral: number;
  horizon: number;
};

export const SCENE_SPEC = {
  width: 100,
  height: 56,
  fps: 12,
  durationSeconds: 6,
  horizon: 30,
  palette: [
    { glyph: " ", color: "#120b09", label: "void" },
    { glyph: ".", color: "#2a1d17", label: "wall shadow" },
    { glyph: ",", color: "#433126", label: "wall light" },
    { glyph: ":", color: "#5d4634", label: "floor shadow" },
    { glyph: ";", color: "#76563a", label: "structure" },
    { glyph: "o", color: "#987044", label: "warm structure" },
    { glyph: "x", color: "#b18853", label: "warm light" },
    { glyph: "%", color: "#c9a66b", label: "brand light" },
    { glyph: "#", color: "#8fc5ac", label: "active green" },
    { glyph: "@", color: "#f3ead9", label: "highlight" },
  ] satisfies ScenePaletteEntry[],
} as const;

export const TERMINAL_LINES = [
  "session / auth-refresh",
  "› keeping the control path attached",
  "  tests  18 passed",
  "  machine  studio-mac",
];

export const PHONE_LINES = [
  "Longhouse",
  "ACTIVE  ·  studio-mac",
  "Send next instruction",
];

export function getSceneCamera(timeSeconds: number): SceneCamera {
  const progress = Math.max(0, Math.min(1, timeSeconds / SCENE_SPEC.durationSeconds));
  const eased = progress * progress * (3 - 2 * progress);
  return {
    // A modest zoom-out makes the shot feel like a camera retreat, not a scale
    // animation. The lateral drift reveals the phone without losing the desk.
    scale: 1.08 - eased * 0.14,
    lateral: eased * 3.2,
    horizon: SCENE_SPEC.horizon,
  };
}
