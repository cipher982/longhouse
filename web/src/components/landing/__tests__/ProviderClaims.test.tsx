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
    expect(screen.getByText("Import, search, and live control")).toBeInTheDocument();
    expect(screen.getByText("Import, search, and control-ready launches through Longhouse")).toBeInTheDocument();
    expect(screen.getByText("Import and search today")).toBeInTheDocument();
    expect(screen.getAllByText("Live now")).toHaveLength(3);
    expect(
      screen.getByText(/Codex and Gemini sessions are already searchable/i),
    ).toBeInTheDocument();
  });

  it("renders FAQ with provider migration question", async () => {
    const user = userEvent.setup();
    render(<TrustSection />);

    await user.click(screen.getByRole("button", { name: /Can I migrate from self-hosted to hosted\?/i }));

    expect(
      screen.getByText(/Export your SQLite database/i),
    ).toBeInTheDocument();
  });
});
