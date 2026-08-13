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

export type TerminalSceneState = {
  key: string;
  status: "WORKING" | "DONE";
  lines: string[];
};

export type PhoneSceneState = {
  key: string;
  status: string;
  message: string;
  detail: string;
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

export function getTerminalSceneState(frameIndex: number): TerminalSceneState {
  if (frameIndex < 18) {
    return {
      key: "edit",
      status: "WORKING",
      lines: ["› update retry loop", "+2  -1"],
    };
  }
  if (frameIndex < 38) {
    return {
      key: "test",
      status: "WORKING",
      lines: ["$ bun test", "18 tests running…"],
    };
  }
  if (frameIndex < 56) {
    return {
      key: "verify",
      status: "WORKING",
      lines: ["✓ 18 tests passed", "2 files changed"],
    };
  }
  return {
    key: "ready",
    status: "DONE",
    lines: ["✓ task complete", "waiting…"],
  };
}

export function getPhoneSceneState(frameIndex: number): PhoneSceneState {
  if (frameIndex < 38) {
    return {
      key: "live",
      status: "LIVE",
      message: "Working",
      detail: "retry loop",
    };
  }
  if (frameIndex < 56) {
    return {
      key: "tests",
      status: "LIVE",
      message: "Passed",
      detail: "18/18",
    };
  }
  return {
    key: "complete",
    status: "DONE",
    message: "Task done",
    detail: "ready",
  };
}

export function getSceneCamera(timeSeconds: number): SceneCamera {
  const progress = Math.max(0, Math.min(1, timeSeconds / SCENE_SPEC.durationSeconds));
  const resolve = smoothCamera(0.4, 1, progress);
  return {
    // Hold the wide shot through the exit, then make one deliberate push toward
    // the workstation and phone. The stable first act keeps the actor readable.
    scale: 1 + resolve * 0.05,
    lateral: resolve * -0.5,
    horizon: SCENE_SPEC.horizon,
  };
}

function smoothCamera(edge0: number, edge1: number, value: number): number {
  const progress = Math.max(0, Math.min(1, (value - edge0) / (edge1 - edge0)));
  return progress * progress * (3 - 2 * progress);
}
