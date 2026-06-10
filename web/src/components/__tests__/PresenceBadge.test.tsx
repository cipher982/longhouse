import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { PresenceBadge, PresenceHero } from "../PresenceBadge";

describe("PresenceBadge styles", () => {
  it("injects presence keyframes once for badge and hero variants", () => {
    render(
      <>
        <PresenceBadge state="thinking" />
        <PresenceHero state="running" tool="bash" />
      </>,
    );

    expect(screen.getByText("Thinking")).toBeInTheDocument();
    expect(screen.getByText("bash")).toBeInTheDocument();
    expect(document.querySelectorAll("#presence-badge-keyframes")).toHaveLength(1);
  });

  it("keeps compact execution badges animated in dense layouts", () => {
    render(
      <>
        <PresenceBadge state="thinking" compact animateCompact />
        <PresenceBadge state="running" tool="shell" compact animateCompact />
      </>,
    );

    const thinkingIndicator = screen.getByTitle("thinking").firstElementChild as HTMLElement;
    const runningIndicator = screen.getByTitle("Running: shell").firstElementChild as HTMLElement;
    expect(thinkingIndicator.style.animation).toContain("presence-pulse");
    expect(runningIndicator.style.animation).toContain("presence-run-blink");
  });

  it("keeps compact needs-user badges steady instead of pulsing like execution", () => {
    render(
      <>
        <PresenceBadge state="needs_user" compact />
        <PresenceBadge state="idle" compact />
      </>,
    );

    const indicator = screen.getByTitle("Idle").firstElementChild as HTMLElement;
    const idleIndicator = screen.getByTitle("idle").firstElementChild as HTMLElement;
    expect(indicator).toBeTruthy();
    expect(indicator.style.animation).toBe("");
    expect(indicator.style.background).toBe(idleIndicator.style.background);
    expect(indicator.style.opacity).toBe(idleIndicator.style.opacity);
  });

  it("keeps compact blocked badges steady instead of pulsing like execution", () => {
    render(<PresenceBadge state="blocked" tool="bash" compact />);

    const indicator = screen.getByTitle("Blocked: bash").firstElementChild as HTMLElement;
    expect(indicator).toBeTruthy();
    expect(indicator.style.animation).toBe("");
  });

  it("renders transcript handoff as working without plumbing copy", () => {
    render(
      <>
        <PresenceBadge state="syncing_transcript" />
        <PresenceBadge state="syncing_transcript" compact animateCompact />
      </>,
    );

    expect(screen.getByText("Working")).toBeInTheDocument();
    expect(screen.queryByText((content) => content.includes("Updating") && content.includes("transcript"))).not.toBeInTheDocument();
    const compactIndicator = screen.getByTitle("Working").firstElementChild as HTMLElement;
    expect(compactIndicator.style.animation).toContain("presence-pulse");
  });
});
