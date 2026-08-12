import { lazy, Suspense, useCallback, useState } from "react";
import { HeroDemo } from "./HeroDemo";
import { liveDemoToken } from "./liveDemoConfig";

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
  live: "Real Claude Code running live in a disposable Linux sandbox. Network and runtime are limited; nothing persists.",
  unavailable:
    "The live session could not start, so this is the recorded demo: real provider CLIs replayed from recordings, with scripted model responses.",
};

const BADGES: Record<Mode, string> = {
  recorded: "Recorded demo",
  live: "Live session",
  unavailable: "Live unavailable",
};

export function HeroDemoShell({ "aria-label": ariaLabel }: { "aria-label": string }) {
  const token = liveDemoToken();
  const [mode, setMode] = useState<Mode>("recorded");

  // Switch the honesty note BEFORE falling back, never after: the page must
  // not describe a recorded replay while a live run is still on screen, or
  // claim live while showing the recording.
  const handleFailure = useCallback(() => setMode("unavailable"), []);

  return (
    <div className="hero-demo-shell">
      <div className="hero-demo-modebar">
        <span className={`hero-demo-badge is-${mode}`}>{BADGES[mode]}</span>
        {token && mode !== "live" && (
          <button
            type="button"
            className="hero-demo-modeswitch"
            onClick={() => setMode("live")}
          >
            Type your own
          </button>
        )}
        {token && mode === "live" && (
          <button
            type="button"
            className="hero-demo-modeswitch"
            onClick={() => setMode("recorded")}
          >
            Back to the recorded demo
          </button>
        )}
      </div>

      {mode === "live" && token ? (
        <Suspense fallback={<div className="hero-live-loading">Loading live runtime…</div>}>
          <LiveDemo token={token} onFailure={handleFailure} />
        </Suspense>
      ) : (
        <HeroDemo aria-label={ariaLabel} />
      )}

      <p className="landing-hero-video-note">{NOTES[mode]}</p>
    </div>
  );
}
