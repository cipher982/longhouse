import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { Header } from "../Header";

describe("Header", () => {
  beforeEach(() => {
    window.history.replaceState({}, "", "/chat");
  });

  it("keeps the launch nav centered on timeline, Oikos, integrations, and runners", () => {
    render(<Header onSync={() => {}} />);

    expect(screen.getByRole("link", { name: "Timeline" })).toHaveAttribute("href", "/timeline");
    expect(screen.getByRole("link", { name: "Oikos" })).toHaveAttribute("href", "/chat");
    expect(screen.getByRole("link", { name: "Integrations" })).toHaveAttribute("href", "/settings/integrations");
    expect(screen.getByRole("link", { name: "Runners" })).toHaveAttribute("href", "/runners");
    expect(screen.queryByRole("link", { name: "Dashboard" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Chat" })).not.toBeInTheDocument();
  });
});
