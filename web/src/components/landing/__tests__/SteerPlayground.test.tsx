import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_INSTRUCTION } from "../demo/LiveDemo";
import { SteerPlayground } from "../SteerPlayground";

vi.mock("@xterm/xterm", () => ({ Terminal: class MockTerminal {} }));
vi.mock("@xterm/addon-fit", () => ({ FitAddon: class MockFitAddon {} }));

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

  it("presents one editable live instruction instead of recorded prompt choices", () => {
    render(<SteerPlayground />);

    const input = screen.getByRole("textbox", { name: "Message to live session" });
    expect(input).toHaveValue(DEFAULT_INSTRUCTION);
    expect(input).not.toHaveAttribute("readonly");
    expect(screen.queryByRole("group", { name: "Choose an instruction" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();

    fireEvent.change(input, { target: { value: "Explain inventory.py" } });
    expect(input).toHaveValue("Explain inventory.py");
  });

  it("keeps the live sandbox below the autoplay hero as its own section", () => {
    render(<SteerPlayground />);

    expect(screen.getByRole("heading", { name: "Send the next move." })).toBeInTheDocument();
    expect(screen.getByText("Real Claude Code")).toBeInTheDocument();
    expect(screen.getByText("Tap to connect")).toBeInTheDocument();
  });
});
