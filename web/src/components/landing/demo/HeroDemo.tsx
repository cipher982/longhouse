import { useMemo } from "react";
import {
  BEATS,
  CROSSFADE_SEC,
  DEMO_DURATION_SEC,
  POSTER_SEC,
  beatWindows,
  type BeatId,
} from "@longhouse/video/demo";
import { useDemoClock } from "./useDemoClock";
import { AgentsBeat } from "./AgentsBeat";
import { UnifyBeat } from "./UnifyBeat";
import { SteerBeat } from "./SteerBeat";
import { CloseBeat } from "./CloseBeat";
import { clamp, clamp01 } from "./ease";

/**
 * The landing hero demo, rendered natively as DOM — no video element, no
 * fixed-aspect player frame. Beats stack in one grid cell and crossfade on
 * a shared looping clock; every layout inside them reflows with the page,
 * so mobile gets readable terminals instead of a uniformly shrunk canvas.
 *
 * The narrative (providers, beat schedule, replay windows) comes from
 * video/src/demo/script.ts, shared with the mp4 export composition.
 */

const BEAT_COMPONENTS: Record<BeatId, React.ComponentType<{ tLocal: number }>> = {
  agents: AgentsBeat,
  unify: UnifyBeat,
  steer: SteerBeat,
  close: CloseBeat,
};

export function HeroDemo({ "aria-label": ariaLabel }: { "aria-label": string }) {
  const { tSec, seek, containerRef } = useDemoClock(DEMO_DURATION_SEC, POSTER_SEC);
  const windows = useMemo(() => beatWindows(), []);

  const activeIndex = windows.reduce(
    (acc, w, i) => (tSec >= w.startSec ? i : acc),
    0,
  );

  return (
    <div ref={containerRef} className="hero-demo" role="group" aria-label={ariaLabel}>
      <div className="hero-demo-stage">
        {windows.map((w, i) => {
          const local = tSec - w.startSec;
          const inWindow = local >= 0 && local < w.durSec;
          const fadeIn = i === 0 ? 1 : clamp01(local / CROSSFADE_SEC);
          const fadeOut =
            i === windows.length - 1
              ? 1
              : clamp01((w.durSec - local) / CROSSFADE_SEC);
          const opacity = inWindow ? Math.min(fadeIn, fadeOut) : 0;
          const BeatComponent = BEAT_COMPONENTS[w.id];
          return (
            <div
              key={w.id}
              className="hero-demo-beat"
              style={{
                opacity,
                visibility: opacity <= 0 ? "hidden" : "visible",
                zIndex: i === activeIndex ? 2 : 1,
              }}
              aria-hidden={i !== activeIndex}
            >
              <BeatComponent tLocal={clamp(local, 0, w.durSec)} />
            </div>
          );
        })}
      </div>
      <div className="hero-demo-footer">
        <p className="hero-demo-caption" key={windows[activeIndex].id}>
          {windows[activeIndex].caption}
        </p>
        <div className="hero-demo-dots" role="tablist" aria-label="Demo parts">
          {windows.map((w, i) => (
            <button
              key={w.id}
              type="button"
              role="tab"
              aria-selected={i === activeIndex}
              className={`hero-demo-dot${i === activeIndex ? " is-active" : ""}`}
              aria-label={`Part ${i + 1} of ${BEATS.length}: ${w.caption}`}
              onClick={() => seek(w.startSec + (i === 0 ? 0 : CROSSFADE_SEC))}
            />
          ))}
        </div>
      </div>
    </div>
  );
}
