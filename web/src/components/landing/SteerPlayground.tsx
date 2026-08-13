import { useCallback, useState } from "react";
import { LiveDemo } from "./demo/LiveDemo";
import { prewarmLiveSession } from "./demo/liveSession";
import "../../styles/steer-playground.css";

export function SteerPlayground() {
  const [active, setActive] = useState(false);

  const handleIntent = useCallback(() => {
    prewarmLiveSession();
    setActive(true);
  }, []);

  return (
    <section
      className="steer-playground"
      id="steer-playground"
      onPointerEnter={handleIntent}
      onFocusCapture={handleIntent}
    >
      <div className="landing-section-inner">
        <div className="steer-playground-body">
          <div className="steer-playground-narrative">
            <p className="steer-playground-kicker">TRY IT LIVE</p>
            <h2>Send the next move.</h2>
            <p className="steer-playground-lead">
              Edit the instruction in the phone, press Send, and watch a real Claude Code
              session carry it out in the terminal.
            </p>
            <div className="steer-playground-live-facts" aria-label="Live demo details">
              <span><i aria-hidden="true" /> Real Claude Code</span>
              <span>Disposable Linux sandbox</span>
            </div>
            <p className="steer-playground-honesty">
              The repository and network are limited for safety. Nothing persists after the
              session ends.
            </p>
          </div>

          <LiveDemo active={active} />
        </div>
      </div>
    </section>
  );
}
