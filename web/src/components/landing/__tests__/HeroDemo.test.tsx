import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { HANDOFF, HERO_CHAPTERS, HERO_POSTER_SEC, HERO_SESSIONS } from "@longhouse/video/demo";
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

  it("starts on the agents chapter with one dot per chapter", () => {
    render(<HeroDemo aria-label="Longhouse demo" />);

    const dots = screen.getAllByRole("button", { name: /^Part / });
    expect(dots).toHaveLength(HERO_CHAPTERS.length);
    expect(dots[0]).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText(HERO_CHAPTERS[0].caption)).toBeInTheDocument();
  });

  it("seeks to a chapter when its dot is clicked", () => {
    render(<HeroDemo aria-label="Longhouse demo" />);

    const dots = screen.getAllByRole("button", { name: /^Part / });
    fireEvent.click(dots[1]);

    expect(dots[1]).toHaveAttribute("aria-pressed", "true");
    expect(dots[0]).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByText(HERO_CHAPTERS[1].caption)).toBeInTheDocument();
  });

  it("freezes on the handoff under reduced motion", () => {
    setReducedMotion(true);

    render(<HeroDemo aria-label="Longhouse demo" />);

    const dots = screen.getAllByRole("button", { name: /^Part / });
    expect(dots[2]).toHaveAttribute("aria-pressed", "true");
    expect(HERO_POSTER_SEC).toBeGreaterThan(HERO_CHAPTERS[2].startSec);
  });
});

describe("hero story data", () => {
  it("tells one story: titles are the prompts the recordings received", () => {
    const [claude, codex, opencode] = HERO_SESSIONS;
    expect(claude.title).toBe(claude.timeline.meta.prompt);
    expect(codex.title).toBe(codex.timeline.meta.prompt);
    expect(opencode.title).toBe(opencode.timeline.meta.prompt);
    expect(new Set(HERO_SESSIONS.map((s) => s.title)).size).toBe(HERO_SESSIONS.length);
  });

  it("sends a genuine follow-up, not the instruction the session already finished", () => {
    expect(HANDOFF.prompt).not.toBe(HERO_SESSIONS[0].title);
    expect(HANDOFF.holdSec).toBeLessThan(HANDOFF.replayStartSec);
    const shown = HANDOFF.reply.map((item) => item.shownSec);
    expect(shown).toEqual([...shown].sort((a, b) => a - b));
    expect(shown.at(-1)).toBeLessThanOrEqual(HANDOFF.replayEndSec);
  });
});
