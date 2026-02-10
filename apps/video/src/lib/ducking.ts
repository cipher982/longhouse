import { interpolate } from "remotion";

interface VoiceoverSegment {
  start: number; // frame
  end: number; // frame
}

interface DuckingConfig {
  segments: VoiceoverSegment[];
  baseVolume?: number; // default 0.35
  duckTo?: number; // default 0.08
  rampFrames?: number; // default 10
  fadeInFrames?: number; // default 60 (2s)
  fadeOutFrames?: number; // default 90 (3s)
  totalFrames: number;
}

export function makeDuckedVolume(
  config: DuckingConfig,
): (frame: number) => number {
  const {
    segments,
    baseVolume = 0.35,
    duckTo = 0.08,
    rampFrames = 10,
    fadeInFrames = 60,
    fadeOutFrames = 90,
    totalFrames,
  } = config;

  return (frame: number) => {
    // Global fade in/out
    const fadeIn = interpolate(frame, [0, fadeInFrames], [0, 1], {
      extrapolateLeft: "clamp",
      extrapolateRight: "clamp",
    });
    const fadeOut = interpolate(
      frame,
      [totalFrames - fadeOutFrames, totalFrames],
      [1, 0],
      { extrapolateLeft: "clamp", extrapolateRight: "clamp" },
    );

    // Duck under voiceover
    let duckFactor = 1;
    for (const seg of segments) {
      const rampDown = interpolate(
        frame,
        [seg.start - rampFrames, seg.start],
        [1, 0],
        { extrapolateLeft: "clamp", extrapolateRight: "clamp" },
      );
      const rampUp = interpolate(
        frame,
        [seg.end, seg.end + rampFrames],
        [0, 1],
        { extrapolateLeft: "clamp", extrapolateRight: "clamp" },
      );

      if (frame >= seg.start - rampFrames && frame <= seg.end + rampFrames) {
        const segDuck =
          frame < seg.start ? rampDown : frame > seg.end ? rampUp : 0;
        duckFactor = Math.min(duckFactor, segDuck);
      }
    }

    const volume = duckTo + (baseVolume - duckTo) * duckFactor;
    return volume * Math.min(fadeIn, fadeOut);
  };
}
