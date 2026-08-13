import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BEATS } from "@longhouse/video/demo";
import { HeroDemo } from "../demo/HeroDemo";

class MockIntersectionObserver {
  observe = vi.fn();
  disconnect = vi.fn();
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

describe("HeroDemo", () => {
  beforeEach(() => {
    setReducedMotion(false);
    vi.stubGlobal("IntersectionObserver", MockIntersectionObserver);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("starts on the agents beat with one dot per beat", () => {
    render(<HeroDemo aria-label="Longhouse demo" />);

    const dots = screen.getAllByRole("button", { name: /^Part / });
    expect(dots).toHaveLength(BEATS.length);
    expect(dots[0]).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText(BEATS[0].caption)).toBeInTheDocument();
  });

  it("seeks to a beat when its dot is clicked", () => {
    render(<HeroDemo aria-label="Longhouse demo" />);

    const dots = screen.getAllByRole("button", { name: /^Part / });
    fireEvent.click(dots[1]);

    expect(dots[1]).toHaveAttribute("aria-pressed", "true");
    expect(dots[0]).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByText(BEATS[1].caption)).toBeInTheDocument();
  });

  it("freezes on the steer beat poster frame under reduced motion", () => {
    setReducedMotion(true);

    render(<HeroDemo aria-label="Longhouse demo" />);

    // POSTER_SEC sits inside the steer beat: the frozen frame shows the
    // instruction already sent and the terminal mid-reaction.
    const dots = screen.getAllByRole("button", { name: /^Part / });
    expect(dots[2]).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText(BEATS[2].caption)).toBeInTheDocument();
  });
});
