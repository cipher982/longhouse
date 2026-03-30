import type { SessionLoopMode } from "../../services/api/agents";

const LOOP_MODE_OPTIONS: Array<{
  value: SessionLoopMode;
  label: string;
  hint: string;
}> = [
  { value: "manual", label: "Manual", hint: "Observe only" },
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
  return (
    <div className="session-pane-section">
      <div className="session-pane-section-title">Loop Mode</div>
      <div
        className="session-loop-mode"
        role="radiogroup"
        aria-label="Session loop mode"
        data-testid="session-loop-mode-group"
      >
        {LOOP_MODE_OPTIONS.map((option) => {
          const isActive = currentMode === option.value;
          return (
            <button
              key={option.value}
              type="button"
              role="radio"
              aria-checked={isActive}
              className={`session-loop-mode__option${isActive ? " is-active" : ""}`}
              onClick={() => onChange?.(option.value)}
              disabled={pending || !onChange}
            >
              <span className="session-loop-mode__label">{option.label}</span>
              <span className="session-loop-mode__hint">{option.hint}</span>
            </button>
          );
        })}
      </div>
      <div className="session-loop-mode__caption">{caption}</div>
    </div>
  );
}
