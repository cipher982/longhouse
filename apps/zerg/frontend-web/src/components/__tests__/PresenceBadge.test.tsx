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
});
