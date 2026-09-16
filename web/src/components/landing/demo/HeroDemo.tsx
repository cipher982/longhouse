import { useEffect, useMemo, useRef, useState } from "react";
import {
  HANDOFF,
  HANDOFF_REPLAY_START_SEC,
  HANDOFF_SENT_SEC,
  HERO_CHAPTERS,
  HERO_DURATION_SEC,
  HERO_POSTER_SEC,
  HERO_SESSIONS,
  HERO_TIMING as T,
} from "@longhouse/video/demo";
import { useDemoClock } from "./useDemoClock";
import { ResponsiveTerminal } from "./ResponsiveTerminal";
import { HeroTimeline } from "./HeroTimeline";
import { HeroPhone } from "./HeroPhone";
import { bezierPoint, heroLayout, placeTransform, type HeroLayout } from "./heroLayout";
import { clamp01 } from "./ease";
import "../../../styles/hero-demo.css";

/**
 * The landing hero: one real session, handed off.
 *
 * Three recorded provider sessions work, dock into a Longhouse timeline,
 * and the Claude session takes a follow-up sent from a phone — replayed
 * from the same recorded PTY. Everything is a pure function of the looping
 * clock's `tSec`; the narrative and every take-coupled number live in
 * video/src/demo/story.ts.
 */

const easeInOut = (p: number) => {
  const x = clamp01(p);
  return x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2;
};
const ramp = (t: number, start: number, dur: number) => easeInOut((t - start) / dur);

function useStageWidth() {
  const ref = useRef<HTMLDivElement | null>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(([entry]) => {
      const next = Math.round(entry?.contentRect.width ?? 0);
      setWidth((prev) => (prev === next ? prev : next));
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  return { ref, width };
}

export function HeroDemo({ "aria-label": ariaLabel }: { "aria-label": string }) {
  const { tSec: t, cycle, seek, containerRef } = useDemoClock(HERO_DURATION_SEC, HERO_POSTER_SEC);
  const { ref: stageRef, width } = useStageWidth();
  const layout = useMemo(() => (width > 0 ? heroLayout(width) : null), [width]);

  const chapterIndex = HERO_CHAPTERS.reduce((acc, c, i) => (t >= c.startSec ? i : acc), 0);
  // Fade in only when the loop wraps; the first paint replaces the static
  // fallback and must not flash an empty stage.
  const loopIn = cycle > 0 ? clamp01(t / T.loopFadeSec) : 1;
  const loopOut = 1 - clamp01((t - (HERO_DURATION_SEC - T.loopFadeSec)) / T.loopFadeSec);

  const handoffP = ramp(t, T.handoffStartSec, T.handoffInDurSec);
  const handoffOn = t >= T.handoffStartSec;
  const replayT = t < HANDOFF_REPLAY_START_SEC
    ? HANDOFF.holdSec
    : Math.min(HANDOFF.replayStartSec + (t - HANDOFF_REPLAY_START_SEC), HANDOFF.replayEndSec);

  return (
    <div
      ref={containerRef}
      className="hero-demo"
      role="group"
      aria-label={ariaLabel}
      data-hero-chapter={HERO_CHAPTERS[chapterIndex].id}
      data-demo-cycle={cycle}
      data-handoff-replay-sec={HANDOFF_REPLAY_START_SEC.toFixed(2)}
    >
      <div
        ref={stageRef}
        className={`hero-stage${layout?.narrow ? " is-narrow" : ""}`}
        style={{ height: layout ? layout.height : undefined }}
      >
        {layout ? (
          <>
            {HERO_SESSIONS.map((session, i) => {
              const dockP = ramp(t, T.dockStartSec + i * T.dockStaggerSec, T.dockDurSec);
              const opacity = loopIn * (1 - clamp01((dockP - 0.72) / 0.28));
              if (opacity <= 0) return null;
              const rect = layout.deck[i];
              const replay = Math.min(
                session.window.startSec + Math.max(0, t - T.replayLeadSec),
                session.window.endSec,
              );
              return (
                <div
                  key={session.id}
                  className="hero-stage-item hero-stage-tile"
                  style={{
                    width: rect.w,
                    zIndex: 25 + i,
                    opacity,
                    transform: placeTransform(rect, rect, layout.thumb(i), dockP),
                  }}
                >
                  <ResponsiveTerminal
                    timeline={session.timeline}
                    tSec={replay}
                    title={session.name}
                    accent={session.accent}
                    detail={session.machine}
                  />
                </div>
              );
            })}

            <HeroTimeline
              layout={layout}
              sessions={HERO_SESSIONS}
              opacity={ramp(t, T.dockStartSec + 0.2, 0.5) * (1 - ramp(t, T.handoffStartSec, 0.45))}
              rowsIn={HERO_SESSIONS.map((_, i) =>
                ramp(t, T.dockStartSec + 0.7 + i * T.dockStaggerSec, 0.4),
              )}
              focus={ramp(t, T.handoffStartSec - 0.7, 0.4)}
            />

            {handoffOn ? (
              <>
                <HeroRelay
                  relay={layout.relay}
                  narrow={layout.narrow}
                  width={layout.width}
                  height={layout.height}
                  opacity={ramp(t, T.handoffStartSec + 0.6, 0.5) * loopOut}
                  travel={clamp01((t - HANDOFF_SENT_SEC) / T.reactDelaySec)}
                  afterglow={1 - 0.6 * ramp(t, HANDOFF_REPLAY_START_SEC + 0.4, 0.8)}
                  sent={t >= HANDOFF_SENT_SEC}
                />
                <div
                  className={`hero-stage-item hero-stage-terminal${
                    t >= HANDOFF_REPLAY_START_SEC && t < HANDOFF_REPLAY_START_SEC + 0.6 ? " is-receiving" : ""
                  }`}
                  style={{
                    width: layout.terminal.w,
                    zIndex: 45,
                    opacity: clamp01((t - T.handoffStartSec) / 0.2) * loopOut,
                    transform: placeTransform(
                      layout.terminal,
                      layout.thumb(0),
                      layout.terminal,
                      handoffP,
                    ),
                  }}
                >
                  <ResponsiveTerminal
                    timeline={HERO_SESSIONS[0].timeline}
                    tSec={replayT}
                    title={HERO_SESSIONS[0].name}
                    accent={HERO_SESSIONS[0].accent}
                    detail={`${HERO_SESSIONS[0].machine} · still at your desk`}
                  />
                </div>
                <div
                  className="hero-stage-item hero-stage-phone"
                  style={{
                    width: layout.phone.w,
                    height: layout.phone.h,
                    zIndex: 40,
                    opacity: ramp(t, T.handoffStartSec + 0.25, 0.5) * loopOut,
                    transform: `translate(${layout.phone.x}px, ${(
                      layout.phone.y + (1 - ramp(t, T.handoffStartSec + 0.25, 0.6)) * 28
                    ).toFixed(2)}px)`,
                  }}
                >
                  <HeroPhone
                    narrow={layout.narrow}
                    width={layout.phone.w}
                    session={HERO_SESSIONS[0]}
                    project={HANDOFF.project}
                    history={HANDOFF.history}
                    prompt={HANDOFF.prompt}
                    typedChars={Math.floor((t - T.typeStartSec) * T.charsPerSec)}
                    sentAgo={t - HANDOFF_SENT_SEC}
                    reply={HANDOFF.reply.filter((item) => t >= HANDOFF_REPLAY_START_SEC && item.shownSec <= replayT)}
                    working={t >= HANDOFF_REPLAY_START_SEC && replayT < HANDOFF.replayEndSec - 0.2}
                  />
                </div>
              </>
            ) : null}
          </>
        ) : null}
      </div>

      <div className="hero-demo-footer">
        <p className="hero-demo-caption" key={HERO_CHAPTERS[chapterIndex].id}>
          {HERO_CHAPTERS[chapterIndex].caption}
        </p>
        <div className="hero-demo-dots" role="group" aria-label="Demo parts">
          {HERO_CHAPTERS.map((chapter, i) => (
            <button
              key={chapter.id}
              type="button"
              aria-pressed={i === chapterIndex}
              className={`hero-demo-dot${i === chapterIndex ? " is-active" : ""}`}
              aria-label={`Part ${i + 1} of ${HERO_CHAPTERS.length}: ${chapter.caption}`}
              onClick={() => seek(chapter.startSec + (i === 0 ? 0.5 : 0.05))}
            />
          ))}
        </div>
      </div>
    </div>
  );
}

/** The message's path: phone Send → Longhouse → the terminal on the desk. */
function HeroRelay({
  relay,
  narrow,
  width,
  height,
  opacity,
  travel,
  afterglow,
  sent,
}: {
  relay: HeroLayout["relay"];
  narrow: boolean;
  width: number;
  height: number;
  opacity: number;
  travel: number;
  afterglow: number;
  sent: boolean;
}) {
  if (opacity <= 0) return null;
  const [a, b, c, d] = relay;
  const path = `M${a.x},${a.y} C${b.x},${b.y} ${c.x},${c.y} ${d.x},${d.y}`;
  const label = bezierPoint(relay, 0.5);
  const pulse = bezierPoint(relay, travel);
  const inFlight = sent && travel < 1;
  return (
    <svg
      className="hero-stage-item hero-relay"
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      style={{ zIndex: 35, opacity }}
      aria-hidden="true"
    >
      <path className="hero-relay-track" d={path} />
      {sent ? (
        <path
          className="hero-relay-lit"
          d={path}
          pathLength={1}
          style={{ strokeDasharray: `${travel} 1`, opacity: afterglow }}
        />
      ) : null}
      {inFlight ? <circle className="hero-relay-pulse" cx={pulse.x} cy={pulse.y} r={5} /> : null}
      <g transform={`translate(${narrow ? label.x - 64 : label.x}, ${label.y})`}>
        <rect className="hero-relay-pill" x={-50} y={-12} width={100} height={24} rx={12} />
        <text className="hero-relay-label" x={0} y={4} textAnchor="middle">
          via Longhouse
        </text>
      </g>
    </svg>
  );
}
