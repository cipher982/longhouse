import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LiveDemo } from "../LiveDemo";

const mocks = vi.hoisted(() => ({
  player: {
    getCurrentFrame: vi.fn(() => 42),
    pause: vi.fn(),
    play: vi.fn(),
    seekTo: vi.fn(),
  },
  intersectionCallback: undefined as
    | ((entries: [{ isIntersecting: boolean }]) => void)
    | undefined,
}));

vi.mock("@longhouse/video", () => ({
  ControlRoom: () => null,
  controlRoomDuration: () => 540,
  FPS: 30,
  HEIGHT: 1080,
  WIDTH: 1920,
}));

vi.mock("@remotion/player", async () => {
  const React = await vi.importActual<typeof import("react")>("react");
  const Player = React.forwardRef<HTMLDivElement, Record<string, unknown>>(
    (props, ref) => {
      React.useImperativeHandle(
        ref,
        () => mocks.player as unknown as HTMLDivElement,
      );
      return React.createElement("div", {
        ref: undefined,
        "data-testid": "player",
        "data-auto-play": String(props.autoPlay),
        "data-initial-frame": String(props.initialFrame),
        "data-duration": String(props.durationInFrames),
        "data-fps": String(props.fps),
        "data-composition-width": String(props.compositionWidth),
        "data-composition-height": String(props.compositionHeight),
        "data-loop": String(props.loop),
        "data-controls": String(props.controls),
      });
    },
  );
  return { Player };
});

class MockIntersectionObserver {
  observe = vi.fn();
  disconnect = vi.fn();

  constructor(callback: (entries: [{ isIntersecting: boolean }]) => void) {
    mocks.intersectionCallback = callback;
  }
}

function setReducedMotion(matches: boolean) {
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => ({
      matches,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  );
}

describe("LiveDemo", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.intersectionCallback = undefined;
    setReducedMotion(false);
    vi.stubGlobal("IntersectionObserver", MockIntersectionObserver);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("passes the live composition dimensions and autoplay props to Player", () => {
    render(<LiveDemo aria-label="Longhouse demo" />);

    expect(screen.getByTestId("player")).toHaveAttribute(
      "data-auto-play",
      "true",
    );
    expect(screen.getByTestId("player")).toHaveAttribute(
      "data-initial-frame",
      "0",
    );
    expect(screen.getByTestId("player")).toHaveAttribute(
      "data-duration",
      "540",
    );
    expect(screen.getByTestId("player")).toHaveAttribute("data-fps", "30");
    expect(screen.getByTestId("player")).toHaveAttribute(
      "data-composition-width",
      "1920",
    );
    expect(screen.getByTestId("player")).toHaveAttribute(
      "data-composition-height",
      "1080",
    );
    expect(screen.getByTestId("player")).toHaveAttribute("data-loop", "true");
    expect(screen.getByTestId("player")).toHaveAttribute(
      "data-controls",
      "false",
    );
  });

  it("freezes at the poster frame when reduced motion is preferred", () => {
    setReducedMotion(true);

    render(<LiveDemo aria-label="Longhouse demo" />);

    expect(screen.getByTestId("player")).toHaveAttribute(
      "data-auto-play",
      "false",
    );
    expect(screen.getByTestId("player")).toHaveAttribute(
      "data-initial-frame",
      "330",
    );
    expect(mocks.player.play).not.toHaveBeenCalled();
  });

  it("pauses offscreen and resumes from the preserved frame", async () => {
    render(<LiveDemo aria-label="Longhouse demo" />);

    expect(mocks.intersectionCallback).toBeDefined();
    mocks.intersectionCallback!([{ isIntersecting: false }]);

    await waitFor(() => expect(mocks.player.pause).toHaveBeenCalled());

    mocks.intersectionCallback!([{ isIntersecting: true }]);
    await waitFor(() => {
      expect(mocks.player.seekTo).toHaveBeenCalledWith(42);
      expect(mocks.player.play).toHaveBeenCalledTimes(2);
    });
  });
});
