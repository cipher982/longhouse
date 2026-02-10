import { SCENE_FRAMES } from "../lib/timing";

interface KenBurnsConfig {
  startScale: number;
  endScale: number;
  startPos: { x: number; y: number };
  endPos: { x: number; y: number };
}

interface OverlayConfig {
  type: "callout";
  text: string;
  enter: number;
  exit: number;
  position: "bottom-left" | "bottom-right";
}

interface SceneConfig {
  clip?: string;
  durationFrames: number;
  voiceover: string;
  kenBurns?: KenBurnsConfig;
  overlays?: OverlayConfig[];
}

export const timelineDemoConfig: { scenes: Record<string, SceneConfig> } = {
  scenes: {
    hook: {
      durationFrames: SCENE_FRAMES.hook,
      voiceover: "clips/timeline-demo/audio/hook.mp3",
    },
    searchWow: {
      clip: "clips/timeline-demo/search-wow.mov",
      durationFrames: SCENE_FRAMES.searchWow,
      voiceover: "clips/timeline-demo/audio/search-wow.mp3",
      kenBurns: {
        startScale: 1.12,
        endScale: 1.0,
        startPos: { x: 0, y: -8 },
        endPos: { x: 0, y: 0 },
      },
      overlays: [
        {
          type: "callout",
          text: "Full-text search",
          enter: 40,
          exit: 120,
          position: "bottom-left",
        },
      ],
    },
    timelineExplore: {
      clip: "clips/timeline-demo/timeline-explore.mov",
      durationFrames: SCENE_FRAMES.timelineExplore,
      voiceover: "clips/timeline-demo/audio/timeline-explore.mp3",
      kenBurns: {
        startScale: 1.0,
        endScale: 1.08,
        startPos: { x: 0, y: 0 },
        endPos: { x: 2, y: -3 },
      },
      overlays: [
        {
          type: "callout",
          text: "Live session timeline",
          enter: 30,
          exit: 140,
          position: "bottom-left",
        },
        {
          type: "callout",
          text: "Tool calls & reasoning",
          enter: 160,
          exit: 280,
          position: "bottom-right",
        },
      ],
    },
    sessionDetail: {
      clip: "clips/timeline-demo/session-detail.mov",
      durationFrames: SCENE_FRAMES.sessionDetail,
      voiceover: "clips/timeline-demo/audio/session-detail.mp3",
      kenBurns: {
        startScale: 1.05,
        endScale: 1.0,
        startPos: { x: -2, y: 0 },
        endPos: { x: 0, y: 0 },
      },
      overlays: [
        {
          type: "callout",
          text: "Deep session inspection",
          enter: 30,
          exit: 160,
          position: "bottom-left",
        },
      ],
    },
    cta: {
      clip: "clips/timeline-demo/cta.mov",
      durationFrames: SCENE_FRAMES.cta,
      voiceover: "clips/timeline-demo/audio/cta.mp3",
      kenBurns: {
        startScale: 1.0,
        endScale: 1.06,
        startPos: { x: 0, y: 0 },
        endPos: { x: 0, y: -2 },
      },
    },
  },
};
