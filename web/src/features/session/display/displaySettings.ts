/**
 * Transcript display settings (session-view-terminal-parity C7). Three
 * presets plus four controls, kept per device. Terminal is the default: the
 * transcript fills the main column at terminal density. Comfortable is the
 * reading layout this view had before (a ~760px column, 15.5px prose, 1.6
 * leading) on desktop. Every preset emits its size and spacing, so phones
 * (which have their own smaller base) order the presets the same way.
 */
import type { CSSProperties } from "react";

export type DisplayWidth = "readable" | "wide" | "full";
export type DisplaySpacing = "compact" | "cozy" | "roomy";
export type DisplayFont = "sans" | "mono";
export type DisplayPresetName = "terminal" | "balanced" | "comfortable";

export interface DisplaySettings {
  width: DisplayWidth;
  size: number;
  spacing: DisplaySpacing;
  font: DisplayFont;
}

export const TEXT_SIZES = [13, 14, 14.5, 15, 15.5, 16, 17] as const;

export const DISPLAY_PRESETS: Record<DisplayPresetName, DisplaySettings> = {
  terminal: { width: "full", size: 14.5, spacing: "compact", font: "sans" },
  balanced: { width: "wide", size: 15, spacing: "cozy", font: "sans" },
  comfortable: { width: "readable", size: 15.5, spacing: "roomy", font: "sans" },
};

export const DEFAULT_DISPLAY_SETTINGS = DISPLAY_PRESETS.terminal;

export const DISPLAY_SETTINGS_STORAGE_KEY = "longhouse.session.display";

const WIDTHS: readonly DisplayWidth[] = ["readable", "wide", "full"];
const SPACINGS: readonly DisplaySpacing[] = ["compact", "cozy", "roomy"];
const FONTS: readonly DisplayFont[] = ["sans", "mono"];

/** Accepts what an older or hand-edited store holds; anything else falls back. */
export function parseDisplaySettings(raw: unknown): DisplaySettings | null {
  if (!raw || typeof raw !== "object") return null;
  const value = raw as Partial<Record<keyof DisplaySettings, unknown>>;
  const width = WIDTHS.includes(value.width as DisplayWidth) ? (value.width as DisplayWidth) : null;
  const spacing = SPACINGS.includes(value.spacing as DisplaySpacing) ? (value.spacing as DisplaySpacing) : null;
  const font = FONTS.includes(value.font as DisplayFont) ? (value.font as DisplayFont) : null;
  const size = typeof value.size === "number" && (TEXT_SIZES as readonly number[]).includes(value.size)
    ? value.size
    : null;
  if (!width || !spacing || !font || size == null) return null;
  return { width, size, spacing, font };
}

export function matchingPreset(settings: DisplaySettings): DisplayPresetName | null {
  for (const [name, preset] of Object.entries(DISPLAY_PRESETS) as [DisplayPresetName, DisplaySettings][]) {
    if (
      preset.width === settings.width &&
      preset.size === settings.size &&
      preset.spacing === settings.spacing &&
      preset.font === settings.font
    ) {
      return name;
    }
  }
  return null;
}

/** One step along TEXT_SIZES, clamped at either end. */
export function stepTextSize(size: number, direction: 1 | -1): number {
  const index = TEXT_SIZES.findIndex((candidate) => candidate >= size);
  const current = index === -1 ? TEXT_SIZES.length - 1 : index;
  const next = Math.min(TEXT_SIZES.length - 1, Math.max(0, current + direction));
  return TEXT_SIZES[next];
}

const SPACING_VARS: Record<DisplaySpacing, Record<string, string>> = {
  compact: {
    "--tl-prose-leading": "1.45",
    "--tl-para-gap": "0.4em",
    "--tl-list-gap": "0.25em",
    "--tl-item-gap": "0.05em",
    "--tl-msg-pad": "8px",
  },
  cozy: {
    "--tl-prose-leading": "1.52",
    "--tl-para-gap": "0.5em",
    "--tl-list-gap": "0.32em",
    "--tl-item-gap": "0.1em",
    "--tl-msg-pad": "10px",
  },
  // The previous desktop values.
  roomy: {
    "--tl-prose-leading": "1.6",
    "--tl-para-gap": "0.6em",
    "--tl-list-gap": "0.4em",
    "--tl-item-gap": "0.15em",
    "--tl-msg-pad": "var(--space-3)",
  },
};

const WIDTH_VARS: Record<DisplayWidth, Record<string, string>> = {
  // Today's responsive column; set nothing.
  readable: {},
  wide: {
    "--tl-prose-max": "120ch",
    "--tl-message-max": "120ch",
    "--tl-tool-max": "130ch",
  },
  // The transcript fills the main column, capped near 190 characters so an
  // ultrawide screen does not run lines across the whole display.
  full: {
    "--tl-prose-max": "190ch",
    "--tl-message-max": "190ch",
    "--tl-tool-max": "190ch",
  },
};

/** Custom properties for the session route. The stylesheet's fallbacks are
 * the previous desktop values, which Comfortable sets explicitly. */
export function displaySettingsStyle(settings: DisplaySettings): CSSProperties {
  const vars: Record<string, string> = {
    ...WIDTH_VARS[settings.width],
    ...SPACING_VARS[settings.spacing],
  };
  vars["--tl-prose-size"] = `${settings.size}px`;
  vars["--tl-ask-size"] = `${settings.size + 0.5}px`;
  if (settings.font === "mono") {
    vars["--tl-prose-font"] = "var(--font-family-mono)";
  }
  return vars as CSSProperties;
}
