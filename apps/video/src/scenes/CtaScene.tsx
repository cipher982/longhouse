import React from "react";
import {
  AbsoluteFill,
  OffthreadVideo,
  staticFile,
  useCurrentFrame,
  interpolate,
} from "remotion";
import { KenBurns } from "../components/KenBurns";
import { KineticText } from "../components/KineticText";
import { Wordmark } from "../components/Wordmark";
import { Vignette } from "../components/Vignette";
import { timelineDemoConfig } from "../data/timeline-demo";
import { SCENE_FRAMES } from "../lib/timing";

const config = timelineDemoConfig.scenes.cta;

export const CtaScene: React.FC = () => {
  const frame = useCurrentFrame();
  const duration = SCENE_FRAMES.cta;
  const kb = config.kenBurns!;

  // Overlay text fades in after a beat
  const textEnter = 30;
  const textOpacity = interpolate(frame, [textEnter, textEnter + 15], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

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

      {/* Darken overlay for text legibility */}
      <AbsoluteFill
        style={{
          background:
            "linear-gradient(to top, rgba(0,0,0,0.6) 0%, transparent 50%)",
        }}
      />

      {/* CTA text overlay */}
      <AbsoluteFill
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "flex-end",
          paddingBottom: 120,
          gap: 24,
          opacity: textOpacity,
          fontFamily:
            '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
        }}
      >
        <Wordmark enterFrame={textEnter} opacity={0.9} fontSize={20} />
        <div style={{ marginTop: 8 }}>
          <KineticText
            text="One command. Fully self-hosted."
            fontSize={36}
            fontWeight={500}
            color="rgba(255, 255, 255, 0.85)"
            staggerFrames={3}
          />
        </div>
      </AbsoluteFill>

      <Vignette intensity={0.4} />
    </AbsoluteFill>
  );
};
