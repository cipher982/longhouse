import React, { useMemo } from "react";
import {
  AbsoluteFill,
  Audio,
  Sequence,
  staticFile,
  useCurrentFrame,
} from "remotion";
import {
  TransitionSeries,
  linearTiming,
} from "@remotion/transitions";
import { fade } from "@remotion/transitions/fade";
import { slide } from "@remotion/transitions/slide";

import { HookScene } from "../scenes/HookScene";
import { SearchWowScene } from "../scenes/SearchWowScene";
import { TimelineExploreScene } from "../scenes/TimelineExploreScene";
import { SessionDetailScene } from "../scenes/SessionDetailScene";
import { CtaScene } from "../scenes/CtaScene";
import {
  FPS,
  SCENE_FRAMES,
  TRANSITION_FRAMES,
  SCENE_STARTS,
  AUDIO_DURATIONS,
  sec,
} from "../lib/timing";
import { makeDuckedVolume } from "../lib/ducking";
import { timelineDemoConfig } from "../data/timeline-demo";

const TRANSITION_MS = (TRANSITION_FRAMES / FPS) * 1000;

const scenes = [
  {
    key: "hook",
    component: HookScene,
    durationInFrames: SCENE_FRAMES.hook,
    transition: fade(),
  },
  {
    key: "searchWow",
    component: SearchWowScene,
    durationInFrames: SCENE_FRAMES.searchWow,
    transition: fade(),
  },
  {
    key: "timelineExplore",
    component: TimelineExploreScene,
    durationInFrames: SCENE_FRAMES.timelineExplore,
    transition: slide({ direction: "from-right" }),
  },
  {
    key: "sessionDetail",
    component: SessionDetailScene,
    durationInFrames: SCENE_FRAMES.sessionDetail,
    transition: fade(),
  },
  {
    key: "cta",
    component: CtaScene,
    durationInFrames: SCENE_FRAMES.cta,
    transition: slide({ direction: "from-bottom" }),
  },
] as const;

const voiceovers = [
  { key: "hook", file: timelineDemoConfig.scenes.hook.voiceover },
  { key: "searchWow", file: timelineDemoConfig.scenes.searchWow.voiceover },
  {
    key: "timelineExplore",
    file: timelineDemoConfig.scenes.timelineExplore.voiceover,
  },
  {
    key: "sessionDetail",
    file: timelineDemoConfig.scenes.sessionDetail.voiceover,
  },
  { key: "cta", file: timelineDemoConfig.scenes.cta.voiceover },
];

export const TimelineDemo: React.FC = () => {
  const frame = useCurrentFrame();

  // Build voiceover segments for music ducking
  const voiceoverSegments = useMemo(() => {
    return voiceovers.map((vo) => {
      const start = SCENE_STARTS[vo.key] ?? 0;
      const audioDuration =
        AUDIO_DURATIONS[vo.key as keyof typeof AUDIO_DURATIONS];
      return {
        start,
        end: start + sec(audioDuration),
      };
    });
  }, []);

  const totalFrames =
    Object.values(SCENE_FRAMES).reduce((a, b) => a + b, 0) -
    (scenes.length - 1) * TRANSITION_FRAMES;

  const musicVolume = useMemo(() => {
    return makeDuckedVolume({
      segments: voiceoverSegments,
      baseVolume: 0.3,
      duckTo: 0.06,
      rampFrames: 12,
      fadeInFrames: sec(2),
      fadeOutFrames: sec(3),
      totalFrames,
    });
  }, [voiceoverSegments, totalFrames]);

  return (
    <AbsoluteFill style={{ backgroundColor: "#0a0a0f" }}>
      {/* Scene transitions */}
      <TransitionSeries>
        {scenes.map((scene, i) => {
          const Scene = scene.component;
          const items: React.ReactNode[] = [];

          // Add transition before each scene (except first)
          if (i > 0) {
            items.push(
              <TransitionSeries.Transition
                key={`transition-${scene.key}`}
                presentation={scene.transition}
                timing={linearTiming({
                  durationInFrames: TRANSITION_FRAMES,
                })}
              />,
            );
          }

          items.push(
            <TransitionSeries.Sequence
              key={scene.key}
              durationInFrames={scene.durationInFrames}
            >
              <Scene />
            </TransitionSeries.Sequence>,
          );

          return items;
        })}
      </TransitionSeries>

      {/* Per-scene voiceover audio */}
      {voiceovers.map((vo) => {
        const startFrame = SCENE_STARTS[vo.key] ?? 0;
        return (
          <Sequence key={`vo-${vo.key}`} from={startFrame}>
            <Audio src={staticFile(vo.file)} volume={0.9} />
          </Sequence>
        );
      })}

      {/* Background music with ducking */}
      <Sequence from={0}>
        <Audio
          src={staticFile("audio/music/ambient-tech.mp3")}
          volume={musicVolume}
          loop
        />
      </Sequence>
    </AbsoluteFill>
  );
};
