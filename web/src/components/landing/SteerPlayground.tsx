import { useState } from "react";
import {
  PROVIDERS,
  STEER_REACT_DELAY_SEC,
  claudeAddtest,
  claudeTile,
  recordingPrompt,
  steerWindow,
} from "@longhouse/video/demo";
import { trackAcquisitionEvent } from "../../lib/analytics";
import { ResponsiveTerminal } from "./demo/ResponsiveTerminal";
import { useReplayClock } from "./demo/useReplayClock";

// endSec is the one editorial number per take: freeze on the finished work
// (tests passed + summary + idle composer), BEFORE the recorder types /exit.
export const STEER_OPTIONS = [
  { id: "fix-inventory", grid: claudeTile, endSec: 9.0 },
  { id: "add-empty-shelf-test", grid: claudeAddtest, endSec: 8.5 },
] as const;

export function SteerPlayground() {
  const claude = PROVIDERS[0];
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [sent, setSent] = useState(false);
  const selected = STEER_OPTIONS.find((option) => option.id === selectedId) ?? STEER_OPTIONS[0];
  const prompt = selectedId ? recordingPrompt(selected.grid) : "";
  const window = steerWindow(selected.grid, selected.endSec);
  const replayDuration = window.endSec - window.startSec + STEER_REACT_DELAY_SEC;
  const { tSec, containerRef, start } = useReplayClock(replayDuration, selected.id);

  const handleSelect = (id: string) => {
    setSelectedId(id);
    setSent(false);
    const option = STEER_OPTIONS.find((candidate) => candidate.id === id);
    if (option) {
      trackAcquisitionEvent("chip_select", {
        surface: "landing",
        placement: "steer_playground",
        chip: option.id,
      });
    }
  };

  const handleSend = () => {
    if (!selectedId || sent) return;
    setSent(true);
    start();
    trackAcquisitionEvent("demo_send", {
      surface: "landing",
      placement: "steer_playground",
      chip: selected.id,
    });
  };

  const replayT = sent
    ? Math.min(
        window.startSec + Math.max(0, tSec - STEER_REACT_DELAY_SEC),
        window.endSec,
      )
    : window.holdSec;

  return (
    <section className="steer-playground" id="steer-playground">
      <div ref={containerRef} className="landing-section-inner">
        <div className="steer-playground-heading">
          <div>
            <p className="steer-playground-kicker">STEER IT YOURSELF</p>
            <h2>Send the next move.</h2>
          </div>
          <p>
            Pick an instruction, send it to the session, and watch the recorded Claude Code
            terminal carry it out.
          </p>
        </div>

        <div className="steer-playground-chips" role="group" aria-label="Choose an instruction">
          <span className="steer-playground-chip-label">Try a real instruction</span>
          {STEER_OPTIONS.map((option) => {
            const optionPrompt = recordingPrompt(option.grid);
            const isSelected = option.id === selectedId;
            return (
              <button
                key={option.id}
                type="button"
                className={`steer-playground-chip${isSelected ? " is-selected" : ""}`}
                aria-pressed={isSelected}
                onClick={() => handleSelect(option.id)}
              >
                {optionPrompt}
              </button>
            );
          })}
        </div>

        <div className="steer-playground-replay">
          <div className={`hero-demo-steer-card steer-playground-card${sent ? " is-sent" : ""}`}>
            <div className="hero-demo-steer-meta">
              <span className="hero-demo-steer-task">{claude.task}</span>
              <span className="hero-demo-steer-session">
                <span style={{ color: claude.color }}>{claude.name}</span>
                {" · demo-repo · "}
                <span className="hero-demo-steer-live">recorded</span>
              </span>
            </div>
            <div className="hero-demo-steer-composer">
              <span className={`hero-demo-steer-text${prompt ? "" : " is-placeholder"}`}>
                {prompt || "Choose an instruction below"}
              </span>
              <button
                type="button"
                className={`hero-demo-steer-send steer-playground-send${sent ? " is-sent" : ""}`}
                disabled={!selectedId || sent}
                onClick={handleSend}
              >
                {sent ? "Sent ✓" : "Send"}
              </button>
            </div>
            <span className="hero-demo-steer-origin">sent from your phone</span>
          </div>

          <ResponsiveTerminal
            timeline={selected.grid}
            tSec={replayT}
            title={claude.name}
            accent={claude.color}
            detail="64 × 14 recording — demo-repo"
          />
        </div>

        <p className="steer-playground-honesty">
          Real Claude Code, replayed from a recording. Model responses are scripted.
        </p>
      </div>
    </section>
  );
}
