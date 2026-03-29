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
    expect(screen.getByText("Archive sync, cloud sessions, direct web continuation")).toBeInTheDocument();
    expect(screen.getByText("Archive sync, managed-local browser driving, and cloud session starts")).toBeInTheDocument();
    expect(screen.getByText("Archive sync and cloud sessions; direct web continuation later")).toBeInTheDocument();
    expect(screen.getAllByText("Live now")).toHaveLength(3);
    expect(
      screen.getByText(/Claude currently has the richest hooks and telemetry/i),
    ).toBeInTheDocument();
  });

  it("explains that direct web continuation is still Claude-first", async () => {
    const user = userEvent.setup();
    render(<TrustSection />);

    await user.click(screen.getByRole("button", { name: /What AI coding agents do you support\?/i }));

    expect(
      screen.getByText(/Claude Code currently has the strongest direct web continuation, hooks, and telemetry/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Codex CLI and Gemini CLI already sync into the timeline and can start cloud sessions today/i),
    ).toBeInTheDocument();
  });
});
