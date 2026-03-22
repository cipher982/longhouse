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

  it("keeps compact needs-user badges steady instead of pulsing like execution", () => {
    render(<PresenceBadge state="needs_user" compact />);

    const indicator = screen.getByTitle("Waiting for input").firstElementChild as HTMLElement;
    expect(indicator).toBeTruthy();
    expect(indicator.style.animation).toBe("");
  });

  it("keeps compact blocked badges steady instead of pulsing like execution", () => {
    render(<PresenceBadge state="blocked" tool="bash" compact />);

    const indicator = screen.getByTitle("Blocked: bash").firstElementChild as HTMLElement;
    expect(indicator).toBeTruthy();
    expect(indicator.style.animation).toBe("");
  });
});
