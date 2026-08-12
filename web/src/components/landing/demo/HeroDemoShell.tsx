import { lazy, Suspense, useCallback, useState } from "react";
import { HeroDemo } from "./HeroDemo";
import { prewarmLiveSession } from "./liveSession";

/**
 * Owns which demo the hero is showing. Recorded and live are mutually
 * exclusive children sharing one visual slot — the live terminal is not a
 * beat, and `HeroDemo`'s clock, crossfades and seek dots stay entirely on the
 * recorded side.
 *
 * The mode is stated visibly, not just in fine print. Two demos on one page is
 * fine; ambiguity about which one you are looking at is not.
 */

const LiveDemo = lazy(() =>
  import("./LiveDemo").then(({ LiveDemo: Component }) => ({ default: Component })),
);

type Mode = "recorded" | "live" | "unavailable";

const NOTES: Record<Mode, string> = {
  recorded:
    "Demo shows real provider CLIs replayed from recordings, with scripted model responses.",
  live: "Real Claude Code, isolated in a disposable Linux sandbox. Network and runtime are limited; nothing persists.",
  unavailable:
    "The live session could not start, so this is the recorded demo: real provider CLIs replayed from recordings, with scripted model responses.",
};

const BADGES: Record<Mode, string> = {
  recorded: "Recorded demo",
  live: "Live session",
  unavailable: "Live unavailable",
};

export function HeroDemoShell({ "aria-label": ariaLabel }: { "aria-label": string }) {
  const [mode, setMode] = useState<Mode>("recorded");

  // Warm only when someone reaches the demo itself — pointer entering the
  // card, or focus landing inside it. An earlier version listened page-wide,
  // which meant every visitor who moved their mouse anywhere spun up a
  // container they never used: wasteful, and it burned the per-visitor cap
  // before anyone got to click.
  const onIntent = useCallback(() => {
    prewarmLiveSession();
  }, []);

  // Switch the honesty note BEFORE falling back, never after: the page must
  // not describe a recorded replay while a live run is still on screen, or
  // claim live while showing the recording.
  const handleFailure = useCallback(() => setMode("unavailable"), []);

  return (
    <div
      className="hero-demo-shell"
      onPointerEnter={onIntent}
      onFocusCapture={onIntent}
    >
      <div className="hero-demo-modebar">
        <div className="hero-demo-mode">
          <span className={`hero-demo-badge is-${mode}`}>
            <span className="hero-demo-badge-dot" aria-hidden="true" />
            {BADGES[mode]}
          </span>
          <span className="hero-demo-modehint">
            {mode === "live" ? "Your prompt. A real agent." : "A two-minute product tour."}
          </span>
        </div>
        {mode !== "live" && (
          <button
            type="button"
            className="hero-demo-modeswitch"
            onClick={() => setMode("live")}
          >
            <span>Try it live</span>
            <span className="hero-demo-modeswitch-arrow" aria-hidden="true">↗</span>
          </button>
        )}
        {mode === "live" && (
          <button
            type="button"
            className="hero-demo-modeswitch"
            onClick={() => setMode("recorded")}
          >
            <span className="hero-demo-modeswitch-arrow is-back" aria-hidden="true">↙</span>
            <span>Recorded demo</span>
          </button>
        )}
      </div>

      {mode === "live" ? (
        <Suspense fallback={<div className="hero-live-loading">Loading live runtime…</div>}>
          <LiveDemo onFailure={handleFailure} />
        </Suspense>
      ) : (
        <HeroDemo aria-label={ariaLabel} />
      )}

      <p className="landing-hero-video-note">
        <svg viewBox="0 0 16 16" aria-hidden="true">
          <path d="M8 1.5 13 3.4v3.9c0 3.1-2 5.8-5 7.2-3-1.4-5-4.1-5-7.2V3.4L8 1.5Z" />
          <path d="m5.8 8 1.4 1.4 3-3" />
        </svg>
        <span>{NOTES[mode]}</span>
      </p>
    </div>
  );
}
