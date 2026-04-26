import type { SessionLoopMode } from "../../services/api/agents";

const LOOP_MODE_OPTIONS: Array<{
  value: SessionLoopMode;
  label: string;
  hint: string;
}> = [
  { value: "assist", label: "Assist", hint: "Draft replies for approval" },
  { value: "autopilot", label: "Autopilot", hint: "Preview policy only" },
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
        {caption}
      </div>
    </div>
  );
}
