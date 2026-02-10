import React from "react";
import { useCurrentFrame, spring, interpolate, useVideoConfig } from "remotion";

interface FeatureCalloutProps {
  text: string;
  enterFrame: number;
  exitFrame: number;
  position?: "bottom-left" | "bottom-right";
}

export const FeatureCallout: React.FC<FeatureCalloutProps> = ({
  text,
  enterFrame,
  exitFrame,
  position = "bottom-left",
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  // Enter animation
  const enterProgress = spring({
    frame: frame - enterFrame,
    fps,
    config: { damping: 18, stiffness: 120 },
  });

  const translateY = (1 - enterProgress) * 20;
  const enterOpacity = enterProgress;

  // Exit animation
  const exitOpacity = interpolate(
    frame,
    [exitFrame, exitFrame + 10],
    [1, 0],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" },
  );

  const opacity = Math.min(enterOpacity, exitOpacity);

  const positionStyle: React.CSSProperties =
    position === "bottom-right"
      ? { right: 60, bottom: 80 }
      : { left: 60, bottom: 80 };

  return (
    <div
      style={{
        position: "absolute",
        ...positionStyle,
        opacity,
        transform: `translateY(${translateY}px)`,
      }}
    >
      <div
        style={{
          background: "rgba(255, 255, 255, 0.1)",
          border: "1px solid rgba(255, 255, 255, 0.2)",
          borderRadius: 20,
          padding: "8px 20px",
          fontSize: 18,
          fontWeight: 500,
          color: "white",
        }}
      >
        {text}
      </div>
    </div>
  );
};
