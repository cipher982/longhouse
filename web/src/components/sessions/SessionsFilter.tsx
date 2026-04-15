/**
 * Filter UI components for the Sessions / Timeline page.
 * FilterChip, FilterSection, DaysSection, FilterPopover.
 */

import { useState, useEffect, useRef } from "react";
import { useClickOutside } from "../../hooks/useClickOutside";
import { useEscapeKey } from "../../hooks/useEscapeKey";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const DAYS_OPTIONS = [7, 14, 30, 60, 90] as const;

// ---------------------------------------------------------------------------
// FilterChip
// ---------------------------------------------------------------------------

export function FilterChip({ label, onDismiss }: { label: string; onDismiss: () => void }) {
  return (
    <div className="sessions-filter-chip">
      <span className="sessions-filter-chip-label">{label}</span>
      <button
        type="button"
        className="sessions-filter-chip-dismiss"
        onClick={onDismiss}
        aria-label={`Remove ${label} filter`}
      >
        <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
          <line x1="18" y1="6" x2="6" y2="18" />
          <line x1="6" y1="6" x2="18" y2="18" />
        </svg>
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// FilterSection
// ---------------------------------------------------------------------------

export function FilterSection({
  label,
  value,
  options,
  onChange,
  loading,
}: {
  label: string;
  value: string;
  options: string[];
  onChange: (v: string) => void;
  loading?: boolean;
}) {
  if (options.length === 0 && !loading) return null;
  return (
    <div className="filter-section" data-filter-section={label.toLowerCase()}>
      <div className="filter-section-label">{label}</div>
      <div className="filter-section-options">
        <button
          type="button"
          className={`filter-option-btn${!value ? " filter-option-btn--active" : ""}`}
          onClick={() => onChange("")}
        >
          All
        </button>
        {options.map((opt) => (
          <button
            key={opt}
            type="button"
            className={`filter-option-btn${value === opt ? " filter-option-btn--active" : ""}`}
            onClick={() => onChange(opt)}
            data-filter-option={opt}
          >
            {opt}
          </button>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// DaysSection
// ---------------------------------------------------------------------------

export function DaysSection({ value, onChange }: { value: number; onChange: (v: number) => void }) {
  return (
    <div className="filter-section" data-filter-section="time">
      <div className="filter-section-label">Time window</div>
      <div className="filter-section-options">
        {DAYS_OPTIONS.map((days) => (
          <button
            key={days}
            type="button"
            className={`filter-option-btn${value === days ? " filter-option-btn--active" : ""}`}
            onClick={() => onChange(days)}
            data-filter-option={`${days}d`}
          >
            {days}d
          </button>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// FilterPopover
// ---------------------------------------------------------------------------

export interface FilterPopoverProps {
  anchorRef: React.RefObject<HTMLButtonElement | null>;
  onClose: () => void;
  project: string; setProject: (v: string) => void; projectOptions: string[];
  provider: string; setProvider: (v: string) => void; providerOptions: string[];
  environment: string; setEnvironment: (v: string) => void; machineOptions: string[];
  daysBack: number; setDaysBack: (v: number) => void;
  hideAutonomous: boolean; setHideAutonomous: (v: boolean) => void;
  filtersLoading: boolean;
}

export function FilterPopover({
  anchorRef, onClose,
  project, setProject, projectOptions,
  provider, setProvider, providerOptions,
  environment, setEnvironment, machineOptions,
  daysBack, setDaysBack,
  hideAutonomous, setHideAutonomous,
  filtersLoading,
}: FilterPopoverProps) {
  const ref = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ top: number; right: number } | null>(null);

  useClickOutside({
    refs: [ref, anchorRef],
    onClickOutside: onClose,
  });
  useEscapeKey(() => {
    onClose();
  });

  useEffect(() => {
    if (!anchorRef.current) return;
    const rect = anchorRef.current.getBoundingClientRect();
    setPos({ top: rect.bottom + 8, right: window.innerWidth - rect.right });
  }, [anchorRef]);

  if (!pos) return null;

  return (
    <div
      ref={ref}
      id="filter-panel"
      role="dialog"
      aria-label="Session filters"
      className="sessions-filter-popover"
      style={{ top: pos.top, right: pos.right }}
    >
      <FilterSection label="Provider" value={provider} options={providerOptions} onChange={setProvider} loading={filtersLoading} />
      <FilterSection label="Machine" value={environment} options={machineOptions} onChange={setEnvironment} loading={filtersLoading} />
      <FilterSection label="Project" value={project} options={projectOptions} onChange={setProject} loading={filtersLoading} />
      <DaysSection value={daysBack} onChange={setDaysBack} />
      <label className="sessions-filter-toggle-label">
        <input
          type="checkbox"
          checked={!hideAutonomous}
          onChange={(e) => setHideAutonomous(!e.target.checked)}
        />
        show autonomous
      </label>
    </div>
  );
}
