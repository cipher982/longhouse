import { useState } from "react";
import {
  PROVIDERS,
  STEER_REACT_DELAY_SEC,
  claudeAddtest,
  claudeSteer,
  recordingPrompt,
  steerWindow,
} from "@longhouse/video/demo";
import { useMemo } from "react";
import { trackAcquisitionEvent } from "../../lib/analytics";
import { PhoneFrame } from "./demo/PhoneFrame";
import { PhoneSessionScreen } from "./demo/PhoneSessionScreen";
import { ResponsiveTerminal } from "./demo/ResponsiveTerminal";
import { extractSessionEvents } from "./demo/sessionEvents";
import { useReplayClock } from "./demo/useReplayClock";

// endSec is the one editorial number per take: freeze on the finished work
// (tests passed + summary + idle composer), BEFORE the recorder types /exit.
// Both takes are 64x20 — taller than the hero's 64x14 tiles so the terminal
// holds its own next to the phone.
export const STEER_OPTIONS = [
  { id: "fix-inventory", grid: claudeSteer, endSec: 8.8 },
  { id: "add-empty-shelf-test", grid: claudeAddtest, endSec: 9.1 },
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
  const clockRunning = sent && tSec < replayDuration;

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

  // The phone mirrors the session the way the real iOS app does: its
  // transcript rows are extracted from the recording and appear in sync
  // with the terminal replay.
  const sessionEvents = useMemo(() => extractSessionEvents(selected.grid), [selected.grid]);
  const visibleEvents = sent
    ? sessionEvents.filter((event) => event.tSec <= replayT)
    : [];

  return (
    <section className="steer-playground" id="steer-playground">
      <div ref={containerRef} className="landing-section-inner">
        <div className="steer-playground-body">
          {/* Narrative + chips live BESIDE the phone they feed: the chips
              are the phone's suggestions, so they must read as attached
              to it, and the heading sharing the row keeps the whole
              interaction inside one viewport. */}
          <div className="steer-playground-narrative">
            <p className="steer-playground-kicker">STEER IT YOURSELF</p>
            <h2>Send the next move.</h2>
            <p className="steer-playground-lead">
              Pick an instruction, send it to the session, and watch the recorded Claude Code
              terminal carry it out.
            </p>
            <div
              className="steer-playground-chips"
              role="group"
              aria-label="Choose an instruction"
            >
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
            <p className="steer-playground-honesty">
              Real Claude Code, replayed from a recording. Model responses are scripted; phone
              UI recreates the Longhouse iOS app.
            </p>
          </div>

          <PhoneFrame>
            <PhoneSessionScreen
              title="Fix the inventory count bug"
              transcript={{
                assistantLine: "Two tests failing — traced it to the loop bounds in count_items.",
                sentMessage: sent ? prompt : undefined,
                events: visibleEvents,
              }}
              composerText={prompt}
              sent={sent}
              working={clockRunning}
              onSend={handleSend}
            />
          </PhoneFrame>

          <ResponsiveTerminal
            timeline={selected.grid}
            tSec={replayT}
            title={claude.name}
            accent={claude.color}
            detail={`${selected.grid.meta.cols} × ${selected.grid.meta.rows} recording — demo-repo`}
          />
        </div>
      </div>
    </section>
  );
}
