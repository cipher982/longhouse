import type { SessionLoopMode } from "../../services/api/agents";

const LOOP_MODE_OPTIONS: Array<{
  value: SessionLoopMode;
  label: string;
  hint: string;
}> = [
  { value: "assist", label: "Assist", hint: "Suggest and nudge" },
  { value: "autopilot", label: "Autopilot", hint: "Continue bounded turns" },
];

interface LoopModeSelectorProps {
  currentMode: SessionLoopMode;
  caption: string;
  pending?: boolean;
  onChange?: (nextMode: SessionLoopMode) => void;
}

export function LoopModeSelector({
  currentMode,
  caption,
  pending = false,
  onChange,
}: LoopModeSelectorProps) {
  const manualActive = currentMode === "manual";

  return (
    <div className="session-pane-section">
      <div className="session-pane-section-title">Loop Mode</div>
      <div
        className="session-loop-mode"
        role="group"
        aria-label="Primary loop modes"
        data-testid="session-loop-mode-group"
      >
        {LOOP_MODE_OPTIONS.map((option) => {
          const isActive = currentMode === option.value;
          return (
            <button
              key={option.value}
              type="button"
              aria-pressed={isActive}
              className={`session-loop-mode__option${isActive ? " is-active" : ""}`}
              onClick={() => onChange?.(option.value)}
              disabled={pending || !onChange}
              title={option.hint}
            >
              <span className="session-loop-mode__label">{option.label}</span>
            </button>
          );
        })}
      </div>
      <div className="session-loop-mode__caption">
        {manualActive ? "Assistance is off." : caption}
      </div>
      <details className="session-loop-mode__advanced">
        <summary className="session-loop-mode__advanced-summary">
          Advanced
          {manualActive ? " · Assistance off" : ""}
        </summary>
        <div className="session-loop-mode__advanced-body">
          <button
            type="button"
            className={`session-loop-mode__manual-button${manualActive ? " is-active" : ""}`}
            aria-pressed={manualActive}
            onClick={() => onChange?.("manual")}
            disabled={pending || !onChange || manualActive}
          >
            Turn off assistance
          </button>
          <span className="session-loop-mode__manual-copy">
            Manual mode only observes the session. Keep this for debugging or
            provider-cost control.
          </span>
        </div>
      </details>
    </div>
  );
}
