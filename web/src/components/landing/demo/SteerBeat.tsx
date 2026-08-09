import { memo } from "react";
import {
  PROVIDERS,
  STEER_CHARS_PER_SEC,
  STEER_REACT_DELAY_SEC,
  STEER_TYPE_START_SEC,
  claudeTile,
  recordingPrompt,
  steerSentAtSec,
  steerWindow,
} from "@longhouse/video/demo";
import { ResponsiveTerminal } from "./ResponsiveTerminal";
import { ramp } from "./ease";

/**
 * Beat 3: an instruction sent from your phone; the REAL recorded Claude
 * Code session reacts (sandboxed first-run, mock-API lane). The message
 * card is a drawn mock; the terminal below it is literal PTY replay. Both
 * the message text and the replay window come FROM the recording, so the
 * card always shows exactly what the session received.
 */

const MESSAGE = recordingPrompt(claudeTile);
const WINDOW = steerWindow(claudeTile);
const SENT_AT = steerSentAtSec(MESSAGE);

function Beat({ tLocal }: { tLocal: number }) {
  const claude = PROVIDERS[0];
  const cardIn = ramp(tLocal, 0.05, 0.45);

  const shown = Math.max(
    0,
    Math.min(
      MESSAGE.length,
      Math.floor((tLocal - STEER_TYPE_START_SEC) * STEER_CHARS_PER_SEC),
    ),
  );
  const typed = MESSAGE.slice(0, shown);
  const sent = shown >= MESSAGE.length;
  const caretOn = Math.floor(tLocal * 3.75) % 2 === 0;

  // Pre-send: hold on the idle composer. Post-send: roll from AFTER the
  // take's typing segment — a remote send arrives as one paste.
  const replayT = sent
    ? Math.min(
        WINDOW.startSec + Math.max(0, tLocal - (SENT_AT + STEER_REACT_DELAY_SEC)),
        WINDOW.endSec,
      )
    : WINDOW.holdSec;

  return (
    <div className="hero-demo-steer">
      <div
        className={`hero-demo-steer-card${sent ? " is-sent" : ""}`}
        style={{
          opacity: cardIn,
          transform: `translateY(${((1 - cardIn) * 16).toFixed(2)}px)`,
        }}
      >
        <div className="hero-demo-steer-meta">
          <span className="hero-demo-steer-task">{claude.task}</span>
          <span className="hero-demo-steer-session">
            <span style={{ color: claude.color }}>{claude.name}</span>
            {" · "}
            {claude.machine}
            {" · "}
            <span className="hero-demo-steer-live">live</span>
          </span>
        </div>
        <div className="hero-demo-steer-composer">
          <span className="hero-demo-steer-text">
            {typed}
            {!sent && caretOn ? <span className="hero-demo-steer-caret">|</span> : null}
          </span>
          <span className={`hero-demo-steer-send${sent ? " is-sent" : ""}`}>
            {sent ? "Sent ✓" : "Send"}
          </span>
        </div>
        <span className="hero-demo-steer-origin">sent from your phone</span>
      </div>

      <div
        className={`hero-demo-steer-pulse${sent ? " is-active" : ""}`}
        aria-hidden="true"
      />

      <ResponsiveTerminal
        timeline={claudeTile}
        tSec={replayT}
        title={claude.name}
        accent={claude.color}
        detail={`${claude.machine} — still at your desk`}
      />
    </div>
  );
}

export const SteerBeat = memo(Beat);
