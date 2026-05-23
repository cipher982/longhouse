import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { IntegrationsSection } from "../IntegrationsSection";
import { TrustSection } from "../TrustSection";

describe("landing provider claims", () => {
  it("shows differentiated provider cards instead of parity copy", () => {
    render(
      <MemoryRouter>
        <IntegrationsSection />
      </MemoryRouter>,
    );

    expect(screen.getByText("Claude Code")).toBeInTheDocument();
    expect(screen.getByText("OpenCode")).toBeInTheDocument();
    expect(screen.getByText("Archive, search, and strongest control path")).toBeInTheDocument();
    expect(screen.getByText("Archive, search, and Longhouse launch path")).toBeInTheDocument();
    expect(screen.getByText("Archive, launch, and hook-backed phase signals")).toBeInTheDocument();
    expect(screen.getByText("Archive, launch, and managed observe")).toBeInTheDocument();
    expect(screen.getByText("Strongest today")).toBeInTheDocument();
    expect(screen.getByText("Control-ready")).toBeInTheDocument();
    expect(screen.getAllByText("Observe-only today").length).toBeGreaterThanOrEqual(2);
    expect(
      screen.getByText(/Codex launch-through-Longhouse is supported/i),
    ).toBeInTheDocument();
  });

  it("renders FAQ with honest provider capability question", async () => {
    const user = userEvent.setup();
    render(<TrustSection />);

    await user.click(screen.getByRole("button", { name: /Which providers are strongest today\?/i }));

    expect(
      screen.getByText(/Claude is the strongest continuation path today/i),
    ).toBeInTheDocument();
  });
});
