import React from "react";
import {
  AbsoluteFill,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { TransitionSeries, linearTiming } from "@remotion/transitions";
import { fade } from "@remotion/transitions/fade";

import { Vignette } from "../components/Vignette";
import { Wordmark } from "../components/Wordmark";
import { KineticText } from "../components/KineticText";
import { TRANSITION_FRAMES, sec } from "../lib/timing";
import { TerminalGrid, type GridTimeline } from "../terminal/TerminalGrid";
import claudeGridJson from "../assets/terminal/claude.grid.json";
import claudeTileJson from "../assets/terminal/claude-tile.grid.json";
import codexTileJson from "../assets/terminal/codex-tile.grid.json";

const claudeGrid = claudeGridJson as unknown as GridTimeline;
const claudeTile = claudeTileJson as unknown as GridTimeline;
const codexTile = codexTileJson as unknown as GridTimeline;

/**
 * ControlRoom — the hero demo, drawn entirely in code so every frame stays
 * legible at landing-hero scale. Four beats:
 *
 *   1. Agents   — four real provider CLIs, live on your machines
 *   2. Unify    — Longhouse normalizes them into one system
 *   3. Steer    — a phone sends the next instruction; the terminal reacts
 *   4. Close    — wordmark
 *
 * No screenshots, no pan/zoom, no text-card hooks.
 */

const FONT = '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif';
const MONO = '"SF Mono", "JetBrains Mono", Menlo, Consolas, monospace';

const BG = "#0e0a08";
const GOLD = "#C9A66B";
const CREAM = "#F3EAD9";

interface Provider {
  name: string;
  cmd: string;
  color: string;
  machine: string;
  task: string;
}

const PROVIDERS: Provider[] = [
  {
    name: "Claude Code",
    cmd: "claude",
    color: "#E8875A",
    machine: "macbook",
    task: "Fix the inventory count bug",
  },
  {
    name: "Codex",
    cmd: "codex",
    color: "#7BC9A8",
    machine: "devbox",
    task: "Repair release build",
  },
  {
    name: "Cursor Agent",
    cmd: "cursor-agent",
    color: "#9B8CFF",
    machine: "studio",
    task: "Migrate settings page",
  },
  {
    name: "OpenCode",
    cmd: "opencode",
    color: "#6FB7E8",
    machine: "homelab",
    task: "Nightly digest job",
  },
];

const Caption: React.FC<{ text: string; delay?: number }> = ({ text, delay = 8 }) => {
  const frame = useCurrentFrame();
  const opacity = interpolate(frame - delay, [0, 10], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  return (
    <AbsoluteFill
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "flex-end",
        flexDirection: "column",
        paddingBottom: 64,
        fontFamily: FONT,
        opacity,
      }}
    >
      <div style={{ fontSize: 44, fontWeight: 650, color: CREAM, letterSpacing: -0.5 }}>
        {text}
      </div>
    </AbsoluteFill>
  );
};

/* ── Beat 1: real agents, really running ────────────────────────────── */

/** Real recorded sessions at 64x14: replay windows over the dense work. */
const TILES = [
  { provider: 0, timeline: () => claudeTile, startSec: 4.0, endSec: 8.7 },
  { provider: 1, timeline: () => codexTile, startSec: 2.6, endSec: 7.5 },
];

const TILE_CELL_W = 13.5;
const TILE_CELL_H = 29;

const AgentsScene: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  return (
    <AbsoluteFill style={{ background: BG, fontFamily: FONT }}>
      <AbsoluteFill
        style={{
          display: "flex",
          flexDirection: "row",
          gap: 56,
          alignItems: "center",
          justifyContent: "center",
          paddingBottom: 150,
        }}
      >
        {TILES.map(({ provider, timeline, startSec, endSec }, i) => {
          const p = PROVIDERS[provider];
          const enter = spring({
            frame: frame - i * 9,
            fps,
            config: { damping: 16, mass: 0.7 },
          });
          const tSec = Math.min(startSec + Math.max(0, frame - 12) / fps, endSec);
          return (
            <div
              key={p.name}
              style={{
                opacity: enter,
                transform: `translateY(${interpolate(enter, [0, 1], [40, 0])}px)`,
                borderRadius: 14,
                overflow: "hidden",
                background: "#151009",
                border: "1px solid rgba(201, 166, 107, 0.22)",
                boxShadow: "0 24px 70px rgba(0,0,0,0.55)",
              }}
            >
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 10,
                  height: 46,
                  padding: "0 16px",
                  background: "#1c1510",
                  borderBottom: "1px solid rgba(201,166,107,0.14)",
                }}
              >
                {["#ff5f57", "#febc2e", "#28c840"].map((c) => (
                  <span
                    key={c}
                    style={{ width: 12, height: 12, borderRadius: 6, background: c }}
                  />
                ))}
                <span style={{ marginLeft: 8, fontSize: 22, fontWeight: 600, color: p.color }}>
                  {p.name}
                </span>
                <span style={{ marginLeft: "auto", fontSize: 20, color: "rgba(243,234,217,0.45)" }}>
                  {p.machine}
                </span>
              </div>
              <div style={{ padding: "14px 16px" }}>
                <TerminalGrid
                  timeline={timeline()}
                  tSec={tSec}
                  cellW={TILE_CELL_W}
                  cellH={TILE_CELL_H}
                />
              </div>
            </div>
          );
        })}
      </AbsoluteFill>
      <Caption text="Your coding agents already run everywhere." delay={26} />
      <Vignette intensity={0.3} />
    </AbsoluteFill>
  );
};

/* ── Beat 2: unify into one system ──────────────────────────────────── */

const UnifyScene: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const panelIn = spring({ frame, fps, config: { damping: 15, mass: 0.7 } });

  return (
    <AbsoluteFill style={{ background: BG, fontFamily: FONT }}>
      <AbsoluteFill
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          paddingBottom: 150,
        }}
      >
        <div
          style={{
            width: 1240,
            borderRadius: 18,
            overflow: "hidden",
            background: "#151009",
            border: "1px solid rgba(201,166,107,0.26)",
            boxShadow: "0 30px 90px rgba(0,0,0,0.6)",
            opacity: panelIn,
            transform: `scale(${interpolate(panelIn, [0, 1], [0.94, 1])})`,
          }}
        >
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 14,
              height: 64,
              padding: "0 26px",
              background: "#1c1510",
              borderBottom: "1px solid rgba(201,166,107,0.14)",
            }}
          >
            <span style={{ fontSize: 26, fontWeight: 700, color: CREAM }}>Longhouse</span>
            <span style={{ fontSize: 22, color: "rgba(243,234,217,0.5)" }}>
              every agent · every machine
            </span>
            <span
              style={{
                marginLeft: "auto",
                fontSize: 20,
                fontWeight: 600,
                color: GOLD,
                border: `1px solid ${GOLD}55`,
                borderRadius: 999,
                padding: "6px 16px",
              }}
            >
              4 sessions
            </span>
          </div>
          {PROVIDERS.map((p, i) => {
            const rowStart = 10 + i * 9;
            const rowIn = spring({
              frame: frame - rowStart,
              fps,
              config: { damping: 17, mass: 0.6 },
            });
            const live = i === 0;
            return (
              <div
                key={p.name}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 20,
                  padding: "22px 26px",
                  borderBottom:
                    i < PROVIDERS.length - 1 ? "1px solid rgba(201,166,107,0.08)" : "none",
                  opacity: rowIn,
                  transform: `translateX(${interpolate(rowIn, [0, 1], [36, 0])}px)`,
                }}
              >
                <span
                  style={{ width: 14, height: 14, borderRadius: 7, background: p.color }}
                />
                <span style={{ fontSize: 27, fontWeight: 600, color: CREAM, width: 330 }}>
                  {p.task}
                </span>
                <span style={{ fontSize: 24, color: "rgba(243,234,217,0.6)", width: 240 }}>
                  {p.name}
                </span>
                <span style={{ fontSize: 24, color: "rgba(243,234,217,0.45)", width: 170 }}>
                  {p.machine}
                </span>
                <span
                  style={{
                    marginLeft: "auto",
                    fontSize: 21,
                    fontWeight: 600,
                    padding: "6px 16px",
                    borderRadius: 999,
                    color: live ? "#0e0a08" : "rgba(243,234,217,0.65)",
                    background: live ? GOLD : "rgba(243,234,217,0.08)",
                  }}
                >
                  {live ? "live · steerable" : ["2m ago", "8m ago", "31m ago"][i - 1]}
                </span>
              </div>
            );
          })}
        </div>
      </AbsoluteFill>
      <Caption text="Longhouse normalizes all of them into one system." delay={16} />
      <Vignette intensity={0.3} />
    </AbsoluteFill>
  );
};

/* ── Beat 3: steer from the phone; the terminal reacts ──────────────── */

const STEER_MESSAGE = "Fix the off-by-one bug in count_items, then run the tests";

// The recording's prompt lands at ~5.5s; work streams until ~9.05s. The
// replay window opens when the phone's Send fires, so the terminal shows the
// REAL recorded Claude Code session (sandboxed first-run, mock-API lane)
// doing exactly what the phone asked.
const REPLAY_START_SEC = 3.5;
const REPLAY_END_SEC = 8.7;

const SteerScene: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const typeStart = Math.round(0.7 * fps);
  const charsPerFrame = 1.1;
  const shown = Math.max(
    0,
    Math.min(STEER_MESSAGE.length, Math.floor((frame - typeStart) * charsPerFrame)),
  );
  const typed = STEER_MESSAGE.slice(0, shown);
  const sent = shown >= STEER_MESSAGE.length;
  const sentAt = typeStart + Math.ceil(STEER_MESSAGE.length / charsPerFrame);
  const caretOn = Math.floor(frame / 8) % 2 === 0;

  const pulse = sent
    ? interpolate((frame - sentAt) % 26, [0, 13, 26], [0.15, 0.95, 0.15])
    : 0;

  const phoneIn = spring({ frame, fps, config: { damping: 16, mass: 0.7 } });
  const claude = PROVIDERS[0];

  return (
    <AbsoluteFill style={{ background: BG, fontFamily: FONT }}>
      <AbsoluteFill
        style={{
          display: "flex",
          flexDirection: "row",
          alignItems: "center",
          justifyContent: "center",
          gap: 46,
          paddingBottom: 150,
        }}
      >
        {/* iPhone frame with the live session + composer */}
        <div
          style={{
            width: 430,
            borderRadius: 54,
            border: "2px solid #4a4438",
            background: "#080605",
            padding: 12,
            boxShadow: "0 30px 90px rgba(0,0,0,0.65)",
            opacity: phoneIn,
            transform: `translateY(${interpolate(phoneIn, [0, 1], [30, 0])}px)`,
          }}
        >
          <div
            style={{
              borderRadius: 42,
              overflow: "hidden",
              background: "#12100d",
              display: "flex",
              flexDirection: "column",
              height: 760,
            }}
          >
            <div
              style={{
                padding: "26px 24px 16px",
                borderBottom: "1px solid rgba(201,166,107,0.14)",
              }}
            >
              <div style={{ fontSize: 25, fontWeight: 650, color: CREAM }}>
                {claude.task}
              </div>
              <div style={{ marginTop: 6, fontSize: 20, color: "rgba(243,234,217,0.55)" }}>
                <span style={{ color: claude.color, fontWeight: 600 }}>Claude Code</span>
                {"  ·  "}
                {claude.machine}
                {"  ·  "}
                <span style={{ color: "#8FC5AC" }}>live</span>
              </div>
            </div>
            <div
              style={{
                flex: 1,
                padding: "20px 24px",
                display: "flex",
                flexDirection: "column",
                gap: 14,
                fontFamily: MONO,
              }}
            >
              <div style={{ fontSize: 20, lineHeight: 1.45, color: "rgba(243,234,217,0.75)" }}>
                Two tests failing — traced it to the loop bounds in count_items.
              </div>
              <div style={{ fontSize: 20, lineHeight: 1.45, color: "rgba(243,234,217,0.5)" }}>
                Waiting for your next instruction…
              </div>
            </div>
            <div style={{ padding: "0 20px 24px", fontFamily: FONT }}>
              <div
                style={{
                  background: "#1c1510",
                  border: `1px solid ${sent ? GOLD : "rgba(201,166,107,0.3)"}`,
                  borderRadius: 18,
                  padding: "16px 18px",
                  minHeight: 58,
                  fontSize: 21,
                  lineHeight: 1.35,
                  color: CREAM,
                }}
              >
                {typed}
                {!sent && caretOn ? <span style={{ color: GOLD }}>|</span> : null}
              </div>
              <div
                style={{
                  marginTop: 12,
                  alignSelf: "flex-end",
                  display: "flex",
                  justifyContent: "flex-end",
                }}
              >
                <span
                  style={{
                    padding: "9px 22px",
                    borderRadius: 12,
                    fontSize: 19,
                    fontWeight: 650,
                    color: sent ? "#0e0a08" : "rgba(243,234,217,0.5)",
                    background: sent ? GOLD : "rgba(243,234,217,0.08)",
                  }}
                >
                  {sent ? "Sent ✓" : "Send"}
                </span>
              </div>
            </div>
          </div>
        </div>

        {/* control pulse */}
        <div
          style={{
            width: 84,
            height: 4,
            borderRadius: 2,
            background: `linear-gradient(90deg, transparent, ${GOLD}, transparent)`,
            opacity: pulse,
            flexShrink: 0,
          }}
        />

        {/* the real recorded session, reacting: literal PTY output replayed */}
        <div style={{ width: 1240 }}>
          <div
            style={{
              borderRadius: 14,
              overflow: "hidden",
              background: "#151009",
              border: "1px solid rgba(201,166,107,0.22)",
              boxShadow: "0 24px 70px rgba(0,0,0,0.55)",
            }}
          >
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 10,
                height: 46,
                padding: "0 16px",
                background: "#1c1510",
                borderBottom: "1px solid rgba(201,166,107,0.14)",
                fontFamily: FONT,
              }}
            >
              {["#ff5f57", "#febc2e", "#28c840"].map((c) => (
                <span key={c} style={{ width: 12, height: 12, borderRadius: 6, background: c }} />
              ))}
              <span style={{ marginLeft: 8, fontSize: 22, fontWeight: 600, color: claude.color }}>
                Claude Code
              </span>
              <span style={{ marginLeft: "auto", fontSize: 20, color: "rgba(243,234,217,0.45)" }}>
                {claude.machine} — still at your desk
              </span>
            </div>
            <div style={{ padding: "16px 20px" }}>
              <TerminalGrid
                timeline={claudeGrid}
                tSec={
                  sent
                    ? Math.min(
                        REPLAY_START_SEC + Math.max(0, frame - (sentAt + 8)) / fps,
                        REPLAY_END_SEC,
                      )
                    : REPLAY_START_SEC
                }
                cellW={12}
                cellH={26}
              />
            </div>
          </div>
        </div>
      </AbsoluteFill>
      <Caption text="Steer any of them, from anywhere." delay={10} />
      <Vignette intensity={0.3} />
    </AbsoluteFill>
  );
};

/* ── Beat 4: close ───────────────────────────────────────────────────── */

const CloseScene: React.FC = () => {
  const frame = useCurrentFrame();
  return (
    <AbsoluteFill
      style={{
        background: BG,
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: 26,
        fontFamily: FONT,
      }}
    >
      <Wordmark enterFrame={4} opacity={0.96} fontSize={72} />
      <KineticText
        text="Remote control for your coding agents."
        fontSize={38}
        staggerFrames={2}
      />
      <div
        style={{
          display: "flex",
          gap: 26,
          marginTop: 8,
          opacity: interpolate(frame - 22, [0, 10], [0, 1], {
            extrapolateLeft: "clamp",
            extrapolateRight: "clamp",
          }),
        }}
      >
        {PROVIDERS.map((p) => (
          <span
            key={p.name}
            style={{ fontSize: 24, fontWeight: 600, color: p.color }}
          >
            {p.name}
          </span>
        ))}
      </div>
    </AbsoluteFill>
  );
};

/* ── Composition ─────────────────────────────────────────────────────── */

export const CONTROL_ROOM_SCENES = [
  { frames: sec(5), scene: <AgentsScene /> },
  { frames: sec(4), scene: <UnifyScene /> },
  { frames: sec(6.5), scene: <SteerScene /> },
  { frames: sec(2.5), scene: <CloseScene /> },
];

export const controlRoomDuration = (): number =>
  CONTROL_ROOM_SCENES.reduce((acc, s) => acc + s.frames, 0) -
  (CONTROL_ROOM_SCENES.length - 1) * TRANSITION_FRAMES;

export const ControlRoom: React.FC = () => (
  <AbsoluteFill style={{ backgroundColor: BG }}>
    <TransitionSeries>
      {CONTROL_ROOM_SCENES.flatMap(({ frames, scene }, i) => {
        const items: React.ReactNode[] = [];
        if (i > 0) {
          items.push(
            <TransitionSeries.Transition
              key={`t-${i}`}
              presentation={fade()}
              timing={linearTiming({ durationInFrames: TRANSITION_FRAMES })}
            />,
          );
        }
        items.push(
          <TransitionSeries.Sequence key={`s-${i}`} durationInFrames={frames}>
            {scene}
          </TransitionSeries.Sequence>,
        );
        return items;
      })}
    </TransitionSeries>
  </AbsoluteFill>
);
