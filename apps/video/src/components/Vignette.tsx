import React from "react";
import { AbsoluteFill } from "remotion";

interface VignetteProps {
  intensity?: number;
}

export const Vignette: React.FC<VignetteProps> = ({ intensity = 0.6 }) => {
  return (
    <AbsoluteFill
      style={{
        pointerEvents: "none",
        background: `radial-gradient(ellipse at center, transparent 50%, rgba(0, 0, 0, ${intensity}) 100%)`,
      }}
    />
  );
};
