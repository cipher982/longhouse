import React from "react";
import { useCurrentFrame, spring, useVideoConfig } from "remotion";

interface KineticTextProps {
  text: string;
  fontSize?: number;
  fontWeight?: number;
  color?: string;
  staggerFrames?: number;
  lineHeight?: number;
  maxWidth?: string;
  textAlign?: "center" | "left" | "right";
}

export const KineticText: React.FC<KineticTextProps> = ({
  text,
  fontSize = 64,
  fontWeight = 700,
  color = "white",
  staggerFrames = 4,
  lineHeight = 1.2,
  maxWidth = "80%",
  textAlign = "center",
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const words = text.split(" ");

  return (
    <div
      style={{
        display: "flex",
        flexWrap: "wrap",
        justifyContent:
          textAlign === "center"
            ? "center"
            : textAlign === "right"
              ? "flex-end"
              : "flex-start",
        maxWidth,
        lineHeight,
      }}
    >
      {words.map((word, i) => {
        const delay = i * staggerFrames;
        const progress = spring({
          frame: frame - delay,
          fps,
          config: { damping: 18, stiffness: 120 },
        });

        const translateY = (1 - progress) * 20;
        const opacity = progress;

        return (
          <span
            key={i}
            style={{
              display: "inline-block",
              fontSize,
              fontWeight,
              color,
              opacity,
              transform: `translateY(${translateY}px)`,
              marginRight: "0.3em",
              whiteSpace: "nowrap",
            }}
          >
            {word}
          </span>
        );
      })}
    </div>
  );
};
