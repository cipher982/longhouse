import React from "react";
import {
  AbsoluteFill,
  interpolate,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { TransitionSeries, linearTiming } from "@remotion/transitions";
import { fade } from "@remotion/transitions/fade";
import { slide } from "@remotion/transitions/slide";

import { KenBurns } from "../components/KenBurns";
import { KineticText } from "../components/KineticText";
import { Wordmark } from "../components/Wordmark";
import { Vignette } from "../components/Vignette";
import { BrowserFrame, PhoneFrame } from "../components/DeviceFrame";
import {
  type Scene,
  type WedgeSpec,
  defaultWedgeSpec,
  sceneFrames,
} from "../lib/wedgeSpec";
import { TRANSITION_FRAMES } from "../lib/timing";

const FONT = '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif';

/** Caption pinned to the lower third, kinetic word-stagger entrance. */
const Caption: React.FC<{ text: string }> = ({ text }) => (
  <AbsoluteFill
    style={{
      display: "flex",
      alignItems: "center",
      justifyContent: "flex-end",
      flexDirection: "column",
      paddingBottom: 90,
      fontFamily: FONT,
    }}
  >
    <KineticText text={text} fontSize={40} fontWeight={600} staggerFrames={2} />
  </AbsoluteFill>
);

const HookSceneView: React.FC<{ headline: string }> = ({ headline }) => (
  <AbsoluteFill
    style={{
      display: "flex",
      alignItems: "center",
      justifyContent: "center",
      fontFamily: FONT,
      padding: "0 8%",
    }}
  >
    <KineticText text={headline} fontSize={84} fontWeight={700} staggerFrames={3} />
  </AbsoluteFill>
);

const ShotSceneView: React.FC<{
  shot: string;
  frame: "browser" | "phone";
  caption?: string;
  durationInFrames: number;
}> = ({ shot, frame, caption, durationInFrames }) => {
  const src = staticFile(`shots/${shot}`);
  return (
    <AbsoluteFill
      style={{ display: "flex", alignItems: "center", justifyContent: "center" }}
    >
      <KenBurns durationInFrames={durationInFrames} startScale={1.04} endScale={1.12}>
        <AbsoluteFill
          style={{ display: "flex", alignItems: "center", justifyContent: "center" }}
        >
          {frame === "browser" ? (
            <BrowserFrame src={src} />
          ) : (
            <PhoneFrame src={src} />
          )}
        </AbsoluteFill>
      </KenBurns>
      {caption ? <Caption text={caption} /> : null}
      <Vignette intensity={0.35} />
    </AbsoluteFill>
  );
};

/** Phone on the right; the steer message "types in" and a control thread fires. */
const SteerSceneView: React.FC<{
  phoneShot: string;
  message: string;
  caption?: string;
}> = ({ phoneShot, message, caption }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  // Type the message in character-by-character starting ~0.8s in.
  const startFrame = Math.round(0.8 * fps);
  const charsPerFrame = 0.9;
  const shown = Math.max(
    0,
    Math.min(message.length, Math.floor((frame - startFrame) * charsPerFrame)),
  );
  const typed = message.slice(0, shown);
  const sent = shown >= message.length;
  const caretOn = Math.floor(frame / 8) % 2 === 0;

  // Golden "control thread" pulse once the message is sent.
  const threadOpacity = sent
    ? interpolate(frame % 30, [0, 15, 30], [0.2, 0.85, 0.2])
    : 0;

  return (
    <AbsoluteFill style={{ fontFamily: FONT }}>
      {/* Bounded centered row: composer (steer FROM) → thread → phone (steer TO).
          Reserve the lower third for the caption so nothing collides. */}
      <AbsoluteFill
        style={{
          display: "flex",
          flexDirection: "row",
          alignItems: "center",
          justifyContent: "center",
          gap: 56,
          paddingBottom: 160,
        }}
      >
        {/* Composer card (the device you steer FROM) */}
        <div style={{ width: 520, display: "flex", flexDirection: "column", gap: 16 }}>
          <div style={{ fontSize: 22, color: "rgba(255,255,255,0.55)", fontWeight: 500 }}>
            Send to a session on your Mac mini
          </div>
          <div
            style={{
              background: "#16161f",
              border: "1px solid rgba(255,255,255,0.1)",
              borderRadius: 16,
              padding: "20px 22px",
              minHeight: 76,
              fontSize: 25,
              lineHeight: 1.35,
              color: "white",
              display: "flex",
              alignItems: "center",
            }}
          >
            <span>{typed}</span>
            {!sent && caretOn ? (
              <span style={{ color: "#C9A66B", marginLeft: 2 }}>|</span>
            ) : null}
          </div>
          <div
            style={{
              alignSelf: "flex-end",
              padding: "10px 22px",
              borderRadius: 12,
              fontSize: 20,
              fontWeight: 600,
              color: sent ? "#0a0a0f" : "rgba(255,255,255,0.5)",
              background: sent ? "#C9A66B" : "rgba(255,255,255,0.08)",
            }}
          >
            {sent ? "Sent ✓" : "Send"}
          </div>
        </div>

        {/* Golden control thread firing toward the phone */}
        <div
          style={{
            width: 90,
            height: 3,
            borderRadius: 2,
            background: "linear-gradient(90deg, transparent, #C9A66B, transparent)",
            opacity: threadOpacity,
            flexShrink: 0,
          }}
        />

        {/* The phone receiving the steer */}
        <PhoneFrame src={staticFile(`shots/${phoneShot}`)} />
      </AbsoluteFill>

      {caption ? <Caption text={caption} /> : null}
      <Vignette intensity={0.3} />
    </AbsoluteFill>
  );
};

const CtaSceneView: React.FC<{ headline: string; subhead?: string }> = ({
  headline,
  subhead,
}) => (
  <AbsoluteFill
    style={{
      display: "flex",
      flexDirection: "column",
      alignItems: "center",
      justifyContent: "center",
      gap: 20,
      fontFamily: FONT,
    }}
  >
    <Wordmark enterFrame={6} opacity={0.95} fontSize={64} />
    {subhead ? (
      <div style={{ fontSize: 30, color: "rgba(255,255,255,0.6)", fontWeight: 500 }}>
        {subhead}
      </div>
    ) : null}
    {headline && headline !== "Longhouse" ? (
      <KineticText text={headline} fontSize={40} staggerFrames={2} />
    ) : null}
  </AbsoluteFill>
);

const renderScene = (scene: Scene, durationInFrames: number): React.ReactNode => {
  switch (scene.kind) {
    case "hook":
      return <HookSceneView headline={scene.headline} />;
    case "screenshot":
      return (
        <ShotSceneView
          shot={scene.shot}
          frame={scene.frame}
          caption={scene.caption}
          durationInFrames={durationInFrames}
        />
      );
    case "steer":
      return (
        <SteerSceneView
          phoneShot={scene.phoneShot}
          message={scene.message}
          caption={scene.caption}
        />
      );
    case "cta":
      return <CtaSceneView headline={scene.headline} subhead={scene.subhead} />;
  }
};

export const SteerLoop: React.FC<WedgeSpec> = (props) => {
  const spec = props?.scenes?.length ? props : defaultWedgeSpec;

  return (
    <AbsoluteFill style={{ backgroundColor: spec.background }}>
      <TransitionSeries>
        {spec.scenes.flatMap((scene, i) => {
          const frames = sceneFrames(scene);
          const items: React.ReactNode[] = [];
          if (i > 0) {
            items.push(
              <TransitionSeries.Transition
                key={`t-${i}`}
                presentation={i === spec.scenes.length - 1 ? slide({ direction: "from-bottom" }) : fade()}
                timing={linearTiming({ durationInFrames: TRANSITION_FRAMES })}
              />,
            );
          }
          items.push(
            <TransitionSeries.Sequence key={`s-${i}`} durationInFrames={frames}>
              {renderScene(scene, frames)}
            </TransitionSeries.Sequence>,
          );
          return items;
        })}
      </TransitionSeries>
    </AbsoluteFill>
  );
};

/** Total duration in frames for a spec, accounting for transition overlaps. */
export const steerLoopDuration = (spec: WedgeSpec): number => {
  const total = spec.scenes.reduce((acc, s) => acc + sceneFrames(s), 0);
  return Math.max(1, total - (spec.scenes.length - 1) * TRANSITION_FRAMES);
};
