import { useCallback, useRef, useState } from "react";
import { useClickOutside } from "@/shared/hooks/useClickOutside";
import { useEscapeKey } from "@/shared/hooks/useEscapeKey";
import {
  DISPLAY_PRESETS,
  TEXT_SIZES,
  matchingPreset,
  stepTextSize,
  type DisplayFont,
  type DisplayPresetName,
  type DisplaySettings,
  type DisplaySpacing,
  type DisplayWidth,
} from "./displaySettings";
import "./display-settings.css";

const PRESET_LABELS: Record<DisplayPresetName, string> = {
  terminal: "Terminal",
  balanced: "Balanced",
  comfortable: "Comfortable",
};

function Segmented<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: T;
  options: readonly { value: T; label: string }[];
  onChange: (value: T) => void;
}) {
  return (
    <div className="display-settings__group" role="radiogroup" aria-label={label}>
      <span className="display-settings__label">{label}</span>
      <div className="display-settings__segmented">
        {options.map((option) => (
          <button
            key={option.value}
            type="button"
            role="radio"
            aria-checked={value === option.value}
            className={value === option.value ? "is-selected" : undefined}
            onClick={() => onChange(option.value)}
          >
            {option.label}
          </button>
        ))}
      </div>
    </div>
  );
}

/** The "Aa" button in the session header and its settings popover. */
export function DisplaySettingsPopover({
  settings,
  onChange,
  onReplace,
}: {
  settings: DisplaySettings;
  onChange: (patch: Partial<DisplaySettings>) => void;
  onReplace: (settings: DisplaySettings) => void;
}) {
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const close = useCallback(() => setOpen(false), []);
  useClickOutside({ enabled: open, refs: [containerRef], onClickOutside: close });
  useEscapeKey(close, open);
  const preset = matchingPreset(settings);
  const sizeIndex = (TEXT_SIZES as readonly number[]).indexOf(settings.size);

  return (
    <div className="display-settings" ref={containerRef}>
      <button
        type="button"
        className={`timeline-pane__filter-toggle display-settings__toggle${open ? " is-active" : ""}`}
        aria-label="Display settings"
        aria-expanded={open}
        title="Display settings"
        data-testid="display-settings-toggle"
        onClick={() => setOpen((value) => !value)}
      >
        Aa
      </button>
      {open ? (
        <div className="display-settings__popover" role="dialog" aria-label="Display settings" data-testid="display-settings">
          <div className="display-settings__group">
            <span className="display-settings__label">Preset</span>
            <div className="display-settings__presets">
              {(Object.keys(DISPLAY_PRESETS) as DisplayPresetName[]).map((name) => (
                <button
                  key={name}
                  type="button"
                  aria-pressed={preset === name}
                  className={preset === name ? "is-selected" : undefined}
                  onClick={() => onReplace(DISPLAY_PRESETS[name])}
                >
                  {PRESET_LABELS[name]}
                </button>
              ))}
            </div>
          </div>
          <Segmented<DisplayWidth>
            label="Width"
            value={settings.width}
            options={[
              { value: "readable", label: "Readable" },
              { value: "wide", label: "Wide" },
              { value: "full", label: "Full" },
            ]}
            onChange={(width) => onChange({ width })}
          />
          <div className="display-settings__group">
            <span className="display-settings__label">Text size</span>
            <div className="display-settings__stepper">
              <button
                type="button"
                aria-label="Smaller text"
                disabled={sizeIndex <= 0}
                onClick={() => onChange({ size: stepTextSize(settings.size, -1) })}
              >
                −
              </button>
              <output aria-live="polite" data-testid="display-settings-size">
                {settings.size}px
              </output>
              <button
                type="button"
                aria-label="Larger text"
                disabled={sizeIndex === TEXT_SIZES.length - 1}
                onClick={() => onChange({ size: stepTextSize(settings.size, 1) })}
              >
                +
              </button>
            </div>
          </div>
          <Segmented<DisplaySpacing>
            label="Line spacing"
            value={settings.spacing}
            options={[
              { value: "compact", label: "Compact" },
              { value: "cozy", label: "Cozy" },
              { value: "roomy", label: "Roomy" },
            ]}
            onChange={(spacing) => onChange({ spacing })}
          />
          <Segmented<DisplayFont>
            label="Prose font"
            value={settings.font}
            options={[
              { value: "sans", label: "Sans" },
              { value: "mono", label: "Mono" },
            ]}
            onChange={(font) => onChange({ font })}
          />
          <p className="display-settings__hint">⌘+ / ⌘− change the text size. Saved on this device.</p>
        </div>
      ) : null}
    </div>
  );
}
