import React from "react";
import { AbsoluteFill, useCurrentFrame, interpolate, useVideoConfig } from "remotion";
import { KineticText } from "../components/KineticText";
import { Wordmark } from "../components/Wordmark";
import { Vignette } from "../components/Vignette";
import { SCENE_FRAMES } from "../lib/timing";

export const HookScene: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const duration = SCENE_FRAMES.hook;

  // Line 1 starts immediately
  const line1Opacity = frame < duration - 15 ? 1 : interpolate(
    frame, [duration - 15, duration], [1, 0],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" },
  );

  // Line 2 starts after line 1 words settle (~2s)
  const line2Delay = 50; // frames
  const line2Opacity = frame < line2Delay
    ? 0
    : frame < duration - 15
      ? interpolate(frame, [line2Delay, line2Delay + 10], [0, 1], {
          extrapolateLeft: "clamp",
          extrapolateRight: "clamp",
        })
      : interpolate(frame, [duration - 15, duration], [1, 0], {
          extrapolateLeft: "clamp",
          extrapolateRight: "clamp",
        });

  // Wordmark enters after both lines settle
  const wordmarkDelay = 90;

  return (
    <AbsoluteFill
      style={{
        backgroundColor: "#0a0a0f",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: 24,
        fontFamily:
          '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
      }}
    >
      <div style={{ opacity: line1Opacity }}>
        <KineticText
          text="Your AI agents are everywhere."
          fontSize={56}
          fontWeight={600}
          color="rgba(255, 255, 255, 0.95)"
          staggerFrames={3}
        />
      </div>

      <div style={{ opacity: line2Opacity, marginTop: 8 }}>
        {frame >= line2Delay && (
          <KineticText
            text="Your visibility into them is nowhere."
            fontSize={56}
            fontWeight={600}
            color="rgba(255, 255, 255, 0.7)"
            staggerFrames={3}
          />
        )}
      </div>

      <div style={{ marginTop: 40 }}>
        <Wordmark enterFrame={wordmarkDelay} opacity={0.35} fontSize={16} />
      </div>

      <Vignette intensity={0.5} />
    </AbsoluteFill>
  );
};
