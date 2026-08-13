import { lerp, projectPoint, smoothstep, type CameraFrame, type Vec3 } from "./sceneMath";

export type SceneProfileKey = "desktop" | "mobile";

export type ScenePaletteEntry = {
  glyph: string;
  color: string;
  label: string;
  material: number;
};

export type SceneProfile = {
  key: SceneProfileKey;
  width: number;
  height: number;
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

export type SceneOverlayLayout = {
  monitor: { left: number; top: number; width: number; height: number };
  phone: { left: number; top: number; width: number; height: number };
};

export const SCENE_MATERIAL = {
  void: 0,
  ambient: 1,
  structure: 2,
  active: 3,
  highlight: 4,
} as const;

export const SCENE_GLYPHS = [".", "-", "|", "/", "\\", "+", "#", "@"] as const;

const paletteGroups = [
  { material: SCENE_MATERIAL.ambient, color: "#5a4032", label: "room shadow" },
  { material: SCENE_MATERIAL.structure, color: "#c59a5e", label: "warm structure" },
  { material: SCENE_MATERIAL.active, color: "#8fc5ac", label: "active green" },
  { material: SCENE_MATERIAL.highlight, color: "#f3ead9", label: "highlight" },
] as const;

const palette: ScenePaletteEntry[] = [
  { glyph: " ", color: "#120b09", label: "void", material: SCENE_MATERIAL.void },
];
for (const group of paletteGroups) {
  for (const glyph of SCENE_GLYPHS) {
    palette.push({ glyph, color: group.color, label: `${group.label} ${glyph}`, material: group.material });
  }
}

export const SCENE_SPEC = {
  fps: 24,
  durationSeconds: 6,
  samplesPerCell: { x: 2, y: 3 },
  profiles: {
    desktop: { key: "desktop", width: 112, height: 64 },
    mobile: { key: "mobile", width: 72, height: 56 },
  } satisfies Record<SceneProfileKey, SceneProfile>,
  palette,
} as const;

export function getSceneProfile(profileKey: SceneProfileKey): SceneProfile {
  return SCENE_SPEC.profiles[profileKey];
}

export function getSceneCamera(profileKey: SceneProfileKey, timeSeconds: number): CameraFrame {
  const push = smoothstep(2.55, 5.45, timeSeconds);
  const profile = profileKey === "desktop"
    ? {
        startPosition: [8.3, 6, 14] as Vec3,
        endPosition: [5.9, 5.05, 10.7] as Vec3,
        startTarget: [-0.35, 2.45, 1.8] as Vec3,
        endTarget: [1.05, 2.45, 2.05] as Vec3,
        startFov: 46,
        endFov: 40.5,
      }
    : {
        startPosition: [5.9, 5.25, 13.8] as Vec3,
        endPosition: [5.25, 4.65, 9.9] as Vec3,
        startTarget: [-0.9, 2.35, 1.85] as Vec3,
        endTarget: [2.15, 2.35, 2.2] as Vec3,
        startFov: 50,
        endFov: 42,
      };
  return {
    position: lerp(profile.startPosition, profile.endPosition, push),
    target: lerp(profile.startTarget, profile.endTarget, push),
    up: [0, 1, 0],
    verticalFovDegrees: profile.startFov + (profile.endFov - profile.startFov) * push,
  };
}

function projectedPercent(point: Vec3, profileKey: SceneProfileKey, timeSeconds: number): { x: number; y: number } {
  const profile = getSceneProfile(profileKey);
  const sourceWidth = profile.width * SCENE_SPEC.samplesPerCell.x;
  const sourceHeight = profile.height * SCENE_SPEC.samplesPerCell.y;
  const projected = projectPoint(point, getSceneCamera(profileKey, timeSeconds), sourceWidth, sourceHeight);
  return { x: (projected.x / sourceWidth) * 100, y: (projected.y / sourceHeight) * 100 };
}

export function getSceneOverlayLayout(profileKey: SceneProfileKey, timeSeconds: number): SceneOverlayLayout {
  const monitorCenter = projectedPercent([2.45, 3.03, 1.52], profileKey, timeSeconds);
  const phoneCenter = projectedPercent([4.45, 2.25, 2.62], profileKey, timeSeconds);
  const monitorWidth = profileKey === "desktop" ? 19 : 31;
  const monitorHeight = profileKey === "desktop" ? 11.5 : 16.5;
  const phoneWidth = profileKey === "desktop" ? 11.5 : 19;
  const phoneHeight = profileKey === "desktop" ? 16 : 23;
  const phoneInset = profileKey === "desktop" ? 0.8 : 1.5;
  const phoneLeft = Math.max(
    phoneInset,
    Math.min(100 - phoneWidth - phoneInset, phoneCenter.x - phoneWidth / 2),
  );
  return {
    monitor: {
      left: monitorCenter.x - monitorWidth / 2,
      top: monitorCenter.y - monitorHeight / 2,
      width: monitorWidth,
      height: monitorHeight,
    },
    phone: {
      left: phoneLeft,
      top: phoneCenter.y - phoneHeight / 2,
      width: phoneWidth,
      height: phoneHeight,
    },
  };
}

export function getTerminalSceneState(frameIndex: number): TerminalSceneState {
  if (frameIndex < 36) {
    return { key: "edit", status: "WORKING", lines: ["› update retry loop", "+2  -1"] };
  }
  if (frameIndex < 84) {
    return { key: "test", status: "WORKING", lines: ["$ bun test", "18 tests running…"] };
  }
  if (frameIndex < 124) {
    return { key: "verify", status: "WORKING", lines: ["✓ 18 tests passed", "2 files changed"] };
  }
  return { key: "done", status: "DONE", lines: ["✓ task complete", "ready for input"] };
}

export function getPhoneSceneState(frameIndex: number): PhoneSceneState {
  if (frameIndex < 84) {
    return { key: "live", status: "LIVE", message: "Working", detail: "retry" };
  }
  if (frameIndex < 124) {
    return { key: "tests", status: "LIVE", message: "Passed", detail: "18/18" };
  }
  return { key: "complete", status: "DONE", message: "Done", detail: "ready" };
}

export function getSceneCaption(frameIndex: number): string {
  if (frameIndex < 72) return "the machine stays awake";
  if (frameIndex < 124) return "work continues on studio-mac";
  return "task finished while you were away";
}
