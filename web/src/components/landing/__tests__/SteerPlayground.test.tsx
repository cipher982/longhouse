import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { recordingPrompt } from "@longhouse/video/demo";
import { STEER_OPTIONS, SteerPlayground } from "../SteerPlayground";

class MockIntersectionObserver {
  observe = vi.fn();
  disconnect = vi.fn();
}

describe("SteerPlayground", () => {
  beforeEach(() => {
    vi.stubGlobal("IntersectionObserver", MockIntersectionObserver);
    vi.stubGlobal(
      "matchMedia",
      vi.fn(() => ({
        matches: false,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      })),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("uses each recording's prompt as its chip label", () => {
    render(<SteerPlayground />);

    for (const option of STEER_OPTIONS) {
      expect(screen.getByRole("button", { name: recordingPrompt(option.grid) })).toBeInTheDocument();
    }
  });

  it("arms Send with the selected recording prompt and marks it sent", () => {
    render(<SteerPlayground />);

    const option = STEER_OPTIONS[1];
    fireEvent.click(screen.getByRole("button", { name: recordingPrompt(option.grid) }));

    const send = screen.getByRole("button", { name: "Send" });
    expect(send).toBeEnabled();
    expect(screen.getAllByText(recordingPrompt(option.grid))).toHaveLength(2);

    fireEvent.click(send);
    expect(screen.getByRole("button", { name: "Sent ✓" })).toBeDisabled();
  });
});
