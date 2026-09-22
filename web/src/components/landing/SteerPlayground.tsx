import { useCallback, useEffect, useRef, useState } from "react";
import { LiveDemo } from "./demo/LiveDemo";
import { prewarmLiveSession } from "./demo/liveSession";
import "../../styles/steer-playground.css";

/** Input only a person produces; crawlers and prerenderers never do. */
const HUMAN_EVENTS = [
  "pointermove",
  "pointerdown",
  "wheel",
  "touchstart",
  "keydown",
] as const;

/**
 * Warm the sandbox once a real person is within about a screen of the demo,
 * so Claude Code is already up (or nearly) when they reach it. Waiting for a
 * hover on the section itself meant every visitor sat through the whole boot.
 */
function useWarmWhenApproached(onApproach: () => void) {
  const ref = useRef<HTMLElement | null>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof IntersectionObserver === "undefined") return;
    let near = false;
    let human = false;
    let fired = false;
    const maybeFire = () => {
      if (fired || !near || !human) return;
      fired = true;
      onApproach();
    };
    const onHuman = () => {
      human = true;
      maybeFire();
    };
    for (const type of HUMAN_EVENTS)
      window.addEventListener(type, onHuman, { passive: true, once: true });
    const observer = new IntersectionObserver(
      ([entry]) => {
        near = Boolean(entry?.isIntersecting);
        maybeFire();
      },
      { rootMargin: "100% 0px" },
    );
    observer.observe(el);
    return () => {
      observer.disconnect();
      for (const type of HUMAN_EVENTS)
        window.removeEventListener(type, onHuman);
    };
  }, [onApproach]);
  return ref;
}

export function SteerPlayground() {
  const [active, setActive] = useState(false);

  const handleIntent = useCallback(() => {
    prewarmLiveSession();
    setActive(true);
  }, []);

  const sectionRef = useWarmWhenApproached(handleIntent);

  return (
    <section
      ref={sectionRef}
      className="steer-playground"
      id="steer-playground"
      onPointerEnter={handleIntent}
      onFocusCapture={handleIntent}
      onTouchStart={handleIntent}
    >
      <div className="landing-section-inner">
        <div className="steer-playground-body">
          <div className="steer-playground-narrative">
            <p className="steer-playground-kicker">TRY IT LIVE</p>
            <h2>Send the next move.</h2>
            <p className="steer-playground-lead">
              Edit the instruction in the live panel and press Send whenever you
              like. A real Claude Code session picks it up in the terminal as
              soon as it is running.
            </p>
            <div
              className="steer-playground-live-facts"
              aria-label="Live demo details"
            >
              <span>
                <i aria-hidden="true" /> Real Claude Code
              </span>
              <span>Disposable Linux sandbox</span>
            </div>
            <p className="steer-playground-honesty">
              The repository and network are limited for safety. Nothing
              persists after the session ends.
            </p>
          </div>

          <LiveDemo active={active} />
        </div>
      </div>
    </section>
  );
}
