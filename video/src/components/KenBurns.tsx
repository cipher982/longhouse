import React from "react";
import {
  useCurrentFrame,
  useVideoConfig,
  interpolate,
  Easing,
} from "remotion";

interface KenBurnsProps {
  durationInFrames: number;
  startScale?: number;
  endScale?: number;
  startPosition?: { x: number; y: number };
  endPosition?: { x: number; y: number };
  children: React.ReactNode;
}

export const KenBurns: React.FC<KenBurnsProps> = ({
  durationInFrames,
  startScale = 1.0,
  endScale = 1.12,
  startPosition = { x: 0, y: 0 },
  endPosition = { x: 0, y: 0 },
  children,
}) => {
  const frame = useCurrentFrame();
  const { width, height } = useVideoConfig();

  const easing = Easing.inOut(Easing.ease);

  const scale = interpolate(frame, [0, durationInFrames], [startScale, endScale], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing,
  });

  const translateX = interpolate(
    frame,
    [0, durationInFrames],
    [startPosition.x, endPosition.x],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp", easing },
  );

  const translateY = interpolate(
    frame,
    [0, durationInFrames],
    [startPosition.y, endPosition.y],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp", easing },
  );

  return (
    <div style={{ overflow: "hidden", width, height }}>
      <div
        style={{
          width: "100%",
          height: "100%",
          transform: `scale(${scale}) translate(${translateX}%, ${translateY}%)`,
          transformOrigin: "center center",
        }}
      >
        {children}
      </div>
    </div>
  );
};
