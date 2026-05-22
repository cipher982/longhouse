import type { SessionLoopMode } from "../../services/api/agents";

interface LoopModePillProps {
  currentMode: SessionLoopMode;
  pending?: boolean;
  onChange?: (nextMode: SessionLoopMode) => void;
}

const OPTIONS: Array<{ value: SessionLoopMode; label: string; hint: string }> = [
  { value: "assist", label: "Assist", hint: "Draft replies for approval" },
  { value: "autopilot", label: "Autopilot", hint: "Preview policy only" },
];

export function LoopModePill({ currentMode, pending = false, onChange }: LoopModePillProps) {
  return (
    <div
      className="session-loop-mode-pill"
      role="group"
      aria-label="Loop mode"
      data-testid="session-loop-mode-pill"
    >
      {OPTIONS.map((option) => {
        const isActive = currentMode === option.value;
        return (
          <button
            key={option.value}
            type="button"
            aria-pressed={isActive}
            title={option.hint}
            disabled={pending || !onChange}
            className={`session-loop-mode-pill__option${isActive ? " is-active" : ""}`}
            onClick={() => onChange?.(option.value)}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}
