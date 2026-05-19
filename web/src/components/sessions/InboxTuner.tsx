/**
 * InboxTuner — live dimension/typography slider for the inbox layout.
 *
 * Hidden by default. Activate with `?tune=1` in the URL or by pressing
 * `Shift+Alt+T`. While active, values persist to localStorage and feed CSS
 * custom properties on :root so the inbox restyles in place without reload.
 *
 * When inactive (the default), the component is a no-op — it does NOT write
 * to localStorage or to :root, so the baked-in defaults in inbox.css win.
 *
 * Reusable: this is a template for tuners on other pages too. The KNOBS list
 * is the only page-specific bit.
 */

import { useCallback, useEffect, useMemo, useState } from "react";

interface TunerKnob {
  key: string;
  cssVar: string;
  label: string;
  unit: "px" | "" | "%" | "ch";
  min: number;
  max: number;
  step: number;
  defaultValue: number;
}

const KNOBS: TunerKnob[] = [
  { key: "rowFont", cssVar: "--inbox-row-font", label: "Row font", unit: "px", min: 11, max: 18, step: 0.5, defaultValue: 13 },
  { key: "rowPadY", cssVar: "--inbox-row-pad-y", label: "Row pad y", unit: "px", min: 2, max: 14, step: 1, defaultValue: 2 },
  { key: "rowMinH", cssVar: "--inbox-row-min-h", label: "Row height", unit: "px", min: 22, max: 48, step: 1, defaultValue: 29 },
  { key: "repoFont", cssVar: "--inbox-repo-font", label: "Repo font", unit: "px", min: 14, max: 28, step: 1, defaultValue: 22 },
  { key: "repoPadY", cssVar: "--inbox-repo-pad-y", label: "Repo pad y", unit: "px", min: 4, max: 24, step: 1, defaultValue: 7 },
  { key: "metaFont", cssVar: "--inbox-meta-font", label: "Meta font", unit: "px", min: 10, max: 18, step: 0.5, defaultValue: 13 },
  { key: "statusWidth", cssVar: "--inbox-status-width", label: "Status slot", unit: "ch", min: 10, max: 26, step: 1, defaultValue: 13 },
  { key: "sourceWidth", cssVar: "--inbox-source-width", label: "Source slot", unit: "ch", min: 6, max: 22, step: 1, defaultValue: 12 },
  { key: "timeWidth", cssVar: "--inbox-time-width", label: "Time slot", unit: "ch", min: 12, max: 24, step: 1, defaultValue: 16 },
  { key: "titleMaxPct", cssVar: "--inbox-title-max", label: "Title max", unit: "%", min: 25, max: 75, step: 5, defaultValue: 50 },
  { key: "groupGap", cssVar: "--inbox-group-gap", label: "Group gap", unit: "px", min: 0, max: 32, step: 2, defaultValue: 0 },
];

const STORAGE_KEY = "longhouse:inbox-tuner:v1";

function loadStored(): Record<string, number> {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return {};
    return JSON.parse(raw) as Record<string, number>;
  } catch {
    return {};
  }
}

export function InboxTuner() {
  const [open, setOpen] = useState(() => {
    if (typeof window === "undefined") return false;
    return new URLSearchParams(window.location.search).get("tune") === "1";
  });

  const [values, setValues] = useState<Record<string, number>>(() => {
    const stored = loadStored();
    const initial: Record<string, number> = {};
    for (const k of KNOBS) initial[k.key] = stored[k.key] ?? k.defaultValue;
    return initial;
  });

  // Toggle hotkey is always active — flips the panel open without touching styles.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.shiftKey && e.altKey && e.key.toLowerCase() === "t") {
        e.preventDefault();
        setOpen((v) => !v);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // Only write CSS variables / persist while the tuner is OPEN. When closed,
  // the baked-in defaults in inbox.css apply. Closing the tuner clears any
  // overrides so the page snaps back to defaults.
  useEffect(() => {
    if (!open) return;
    const root = document.documentElement;
    for (const k of KNOBS) {
      const v = values[k.key] ?? k.defaultValue;
      root.style.setProperty(k.cssVar, `${v}${k.unit}`);
    }
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(values));
    } catch {
      // ignore quota errors
    }
    return () => {
      for (const k of KNOBS) root.style.removeProperty(k.cssVar);
    };
  }, [open, values]);

  const setValue = useCallback((key: string, value: number) => {
    setValues((prev) => ({ ...prev, [key]: value }));
  }, []);

  const reset = useCallback(() => {
    const initial: Record<string, number> = {};
    for (const k of KNOBS) initial[k.key] = k.defaultValue;
    setValues(initial);
  }, []);

  const cssDump = useMemo(() => {
    return KNOBS.map((k) => {
      const v = values[k.key] ?? k.defaultValue;
      return `  ${k.cssVar}: ${v}${k.unit};`;
    }).join("\n");
  }, [values]);

  if (!open) return null;

  return (
    <div className="inbox-tuner" role="dialog" aria-label="Inbox tuner">
      <header className="inbox-tuner-header">
        <span>Inbox tuner</span>
        <button type="button" className="inbox-tuner-close" onClick={() => setOpen(false)} aria-label="Close tuner">
          ×
        </button>
      </header>

      <div className="inbox-tuner-knobs">
        {KNOBS.map((k) => {
          const v = values[k.key] ?? k.defaultValue;
          return (
            <label key={k.key} className="inbox-tuner-knob">
              <span className="inbox-tuner-knob-label">{k.label}</span>
              <input
                type="range"
                min={k.min}
                max={k.max}
                step={k.step}
                value={v}
                onChange={(e) => setValue(k.key, Number(e.target.value))}
              />
              <span className="inbox-tuner-knob-value">
                {v}
                {k.unit}
              </span>
            </label>
          );
        })}
      </div>

      <footer className="inbox-tuner-footer">
        <button type="button" onClick={reset}>Reset</button>
        <button
          type="button"
          onClick={() => {
            navigator.clipboard.writeText(cssDump).catch(() => {});
          }}
        >
          Copy CSS
        </button>
      </footer>
      <pre className="inbox-tuner-dump">{cssDump}</pre>
    </div>
  );
}
