import React from "react";
import { AbsoluteFill, OffthreadVideo, staticFile } from "remotion";
import { KenBurns } from "../components/KenBurns";
import { FeatureCallout } from "../components/FeatureCallout";
import { Vignette } from "../components/Vignette";
import { timelineDemoConfig } from "../data/timeline-demo";
import { SCENE_FRAMES } from "../lib/timing";

const config = timelineDemoConfig.scenes.sessionDetail;

export const SessionDetailScene: React.FC = () => {
  const duration = SCENE_FRAMES.sessionDetail;
  const kb = config.kenBurns!;

  return (
    <AbsoluteFill>
      <KenBurns
        durationInFrames={duration}
        startScale={kb.startScale}
        endScale={kb.endScale}
        startPosition={kb.startPos}
        endPosition={kb.endPos}
      >
        <OffthreadVideo
          src={staticFile(config.clip!)}
          style={{ width: "100%", height: "100%" }}
        />
      </KenBurns>

      {config.overlays?.map((overlay, i) => (
        <FeatureCallout
          key={i}
          text={overlay.text}
          enterFrame={overlay.enter}
          exitFrame={overlay.exit}
          position={overlay.position}
        />
      ))}

      <Vignette intensity={0.3} />
    </AbsoluteFill>
  );
};
