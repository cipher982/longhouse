import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_INSTRUCTION, LiveDemo } from "../LiveDemo";
import type { LiveSession } from "../liveSession";

vi.mock("@xterm/xterm", () => ({
  Terminal: class MockTerminal {
    cols = 0;
    rows = 0;
    buffer = { active: { baseY: 0, length: 0, getLine: () => null } };
    open = vi.fn();
    loadAddon = vi.fn();
    write = vi.fn();
    dispose = vi.fn();
  },
}));
vi.mock("@xterm/addon-fit", () => ({
  FitAddon: class MockFitAddon {
    fit = vi.fn();
  },
}));

const emptyProjection = {
  root_session_id: "s",
  focus_session_id: "s",
  head_session_id: "s",
  path_session_ids: ["s"],
  items: [],
  total: 0,
  branch_mode: "head" as const,
};

let fakeState: LiveSession["state"] = "ready";
let fakeWatchers: Array<() => void> = [];

function makeFakeSession(): LiveSession {
  const session = {
    state: fakeState,
    failure: null,
    transcript: "",
    send: vi.fn(),
    attach: vi.fn(),
    detach: vi.fn(),
    resize: vi.fn(),
    launch: vi.fn(),
    onChange: vi.fn((watcher: () => void) => {
      fakeWatchers.push(watcher);
      return () => {};
    }),
    events: vi.fn(async () => emptyProjection),
    close: vi.fn(),
  };
  return session as unknown as LiveSession;
}

vi.mock("../liveSession", () => ({
  prewarmLiveSession: vi.fn(() => makeFakeSession()),
  releaseLiveSession: vi.fn(),
}));

class MockIntersectionObserver {
  observe = vi.fn();
  disconnect = vi.fn();
}

class MockResizeObserver {
  observe = vi.fn();
  disconnect = vi.fn();
}

describe("LiveDemo composer", () => {
  beforeEach(() => {
    vi.stubGlobal("IntersectionObserver", MockIntersectionObserver);
    vi.stubGlobal("ResizeObserver", MockResizeObserver);
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
    vi.clearAllMocks();
    fakeState = "ready";
    fakeWatchers = [];
  });

  it("clears the composer exactly once and sends once", async () => {
    const { unmount } = render(<LiveDemo active />);

    // Mount drives the fake session straight to "ready".
    const input = screen.getByRole("textbox", { name: "Message to live session" });
    await act(async () => {
      fireEvent.change(input, { target: { value: "Explain inventory.py" } });
    });
    expect(input).toHaveValue("Explain inventory.py");

    const send = screen.getByRole("button", { name: "Send" });
    await act(async () => {
      fireEvent.click(send);
    });

    const fake = (await import("../liveSession")).prewarmLiveSession as ReturnType<typeof vi.fn>;
    const session = fake.mock.results.at(-1)?.value as LiveSession;

    expect(session.send).toHaveBeenCalledTimes(1);
    expect(session.send).toHaveBeenCalledWith("Explain inventory.py");

    // Composer cleared once, not re-armed: empty value, disabled, send gone.
    expect(input).toHaveValue("");
    expect(input).toBeDisabled();
    expect(screen.getByRole("button", { name: "Message sent" })).toBeDisabled();
    // The submitted instruction is shown as the user bubble, not duplicated
    // from the (empty) projection.
    expect(screen.getByText("Explain inventory.py")).toBeInTheDocument();

    unmount();
  });

  it("does not send the stock instruction until the user edits and sends", async () => {
    render(<LiveDemo active />);

    const input = screen.getByRole("textbox", { name: "Message to live session" });
    await act(async () => {});
    // The composer is pre-filled but the turn has not been submitted yet.
    expect(input).toHaveValue(DEFAULT_INSTRUCTION);

    const fake = (await import("../liveSession")).prewarmLiveSession as ReturnType<typeof vi.fn>;
    const session = fake.mock.results.at(-1)?.value as LiveSession;
    expect(session.send).not.toHaveBeenCalled();
  });

  it("lets the visitor send while Claude is starting and delivers once it is up", async () => {
    fakeState = "launching";
    render(<LiveDemo active />);

    const send = screen.getByRole("button", { name: "Send" });
    expect(send).toBeEnabled();
    await act(async () => {
      fireEvent.click(send);
    });

    const fake = (await import("../liveSession")).prewarmLiveSession as ReturnType<typeof vi.fn>;
    const session = fake.mock.results.at(-1)?.value as LiveSession;
    expect(session.send).not.toHaveBeenCalled();
    expect(screen.getByText(DEFAULT_INSTRUCTION)).toBeInTheDocument();
    expect(screen.getByText("Delivers when Claude Code is up")).toBeInTheDocument();

    await act(async () => {
      (session as { state: string }).state = "ready";
      for (const watcher of fakeWatchers) watcher();
    });
    expect(session.send).toHaveBeenCalledTimes(1);
    expect(session.send).toHaveBeenCalledWith(DEFAULT_INSTRUCTION);
  });
});
