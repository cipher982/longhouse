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
      screen.getByText(/Claude currently has the richest hooks and telemetry/i),
    ).toBeInTheDocument();
  });

  it("explains provider support levels", async () => {
    const user = userEvent.setup();
    render(<TrustSection />);

    await user.click(screen.getByRole("button", { name: /What AI coding agents do you support\?/i }));

    expect(
      screen.getByText(/Claude Code currently has the strongest hooks, telemetry, and live control/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Codex CLI and Gemini CLI already land in the same timeline and machine surface today/i),
    ).toBeInTheDocument();
  });
});
