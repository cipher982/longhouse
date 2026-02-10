import React from "react";
import { useCurrentFrame, interpolate, spring, useVideoConfig } from "remotion";

interface WordmarkProps {
  enterFrame?: number;
  opacity?: number;
  fontSize?: number;
}

export const Wordmark: React.FC<WordmarkProps> = ({
  enterFrame = 0,
  opacity: targetOpacity = 0.4,
  fontSize = 14,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const letterSpacing = spring({
    frame: frame - enterFrame,
    fps,
    config: { damping: 200, stiffness: 120 },
  }) * 12;

  const opacity = interpolate(
    frame,
    [enterFrame, enterFrame + 20],
    [0, targetOpacity],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" },
  );

  return (
    <div
      style={{
        fontSize,
        fontWeight: 600,
        color: "white",
        opacity,
        letterSpacing,
        textTransform: "uppercase" as const,
      }}
    >
      LONGHOUSE
    </div>
  );
};
