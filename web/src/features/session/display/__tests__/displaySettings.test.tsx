import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import {
  DEFAULT_DISPLAY_SETTINGS,
  DISPLAY_PRESETS,
  DISPLAY_SETTINGS_STORAGE_KEY,
  displaySettingsStyle,
  matchingPreset,
  parseDisplaySettings,
  stepTextSize,
} from "../displaySettings";
import { DisplaySettingsPopover } from "../DisplaySettingsPopover";
import { useDisplaySettings } from "../useDisplaySettings";

function Harness() {
  const display = useDisplaySettings();
  return (
    <div>
      <DisplaySettingsPopover settings={display.settings} onChange={display.update} onReplace={display.replace} />
      <output data-testid="current">{JSON.stringify(display.settings)}</output>
    </div>
  );
}

function current() {
  return JSON.parse(screen.getByTestId("current").textContent ?? "{}");
}

describe("display settings", () => {
  afterEach(() => {
    window.localStorage.removeItem(DISPLAY_SETTINGS_STORAGE_KEY);
  });

  it("defaults to Terminal; Comfortable keeps the old width and sets the old desktop type", () => {
    expect(DEFAULT_DISPLAY_SETTINGS).toEqual(DISPLAY_PRESETS.terminal);
    const comfortable = displaySettingsStyle(DISPLAY_PRESETS.comfortable) as Record<string, string>;
    expect(comfortable["--tl-tool-max"]).toBeUndefined();
    expect(comfortable).toMatchObject({
      "--tl-prose-size": "15.5px",
      "--tl-ask-size": "16px",
      "--tl-prose-leading": "1.6",
      "--tl-para-gap": "0.6em",
    });
    expect(displaySettingsStyle(DISPLAY_PRESETS.terminal)).toMatchObject({
      "--tl-tool-max": "190ch",
      "--tl-prose-size": "14.5px",
      "--tl-prose-leading": "1.45",
    });
  });

  it("rejects stored values it does not recognise", () => {
    expect(parseDisplaySettings({ width: "huge", size: 14.5, spacing: "compact", font: "sans" })).toBeNull();
    expect(parseDisplaySettings({ width: "full", size: 99, spacing: "compact", font: "sans" })).toBeNull();
    expect(parseDisplaySettings("terminal")).toBeNull();
    expect(parseDisplaySettings(DISPLAY_PRESETS.balanced)).toEqual(DISPLAY_PRESETS.balanced);
  });

  it("steps text size along the scale and clamps at the ends", () => {
    expect(stepTextSize(14.5, 1)).toBe(15);
    expect(stepTextSize(14.5, -1)).toBe(14);
    expect(stepTextSize(13, -1)).toBe(13);
    expect(stepTextSize(17, 1)).toBe(17);
    expect(matchingPreset({ ...DISPLAY_PRESETS.terminal, size: 16 })).toBeNull();
  });

  it("applies a preset and a control from the popover and keeps them on this device", async () => {
    const user = userEvent.setup();
    const first = render(<Harness />);
    expect(current()).toEqual(DISPLAY_PRESETS.terminal);

    await user.click(screen.getByTestId("display-settings-toggle"));
    await user.click(screen.getByRole("button", { name: "Comfortable" }));
    await user.click(screen.getByRole("radio", { name: "Mono" }));
    expect(current()).toEqual({ ...DISPLAY_PRESETS.comfortable, font: "mono" });
    first.unmount();

    render(<Harness />);
    expect(current()).toEqual({ ...DISPLAY_PRESETS.comfortable, font: "mono" });
  });

  it("steps the text size with Command-plus and Command-minus", () => {
    render(<Harness />);
    fireEvent.keyDown(window, { key: "=", metaKey: true });
    expect(current().size).toBe(15);
    fireEvent.keyDown(window, { key: "-", ctrlKey: true });
    fireEvent.keyDown(window, { key: "-", ctrlKey: true });
    expect(current().size).toBe(14);
    fireEvent.keyDown(window, { key: "-" });
    expect(current().size).toBe(14);

    const modal = document.createElement("div");
    modal.setAttribute("aria-modal", "true");
    document.body.appendChild(modal);
    try {
      fireEvent.keyDown(window, { key: "=", metaKey: true });
      expect(current().size).toBe(14);
    } finally {
      modal.remove();
    }
  });
});
