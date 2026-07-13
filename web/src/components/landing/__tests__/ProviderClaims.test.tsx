import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { IntegrationsSection } from "../IntegrationsSection";
import { TrustSection } from "../TrustSection";

describe("landing provider claims", () => {
  it("shows plain provider capabilities and limitations", () => {
    render(
      <MemoryRouter>
        <IntegrationsSection />
      </MemoryRouter>,
    );

    expect(screen.getByText("Claude Code")).toBeInTheDocument();
    expect(screen.getByText("OpenCode")).toBeInTheDocument();
    expect(screen.getAllByText("Launch, send, steer, interrupt, and resume")).toHaveLength(2);
    expect(screen.getByText("Launch, send, interrupt, and terminate")).toBeInTheDocument();
    expect(screen.getByText("Local launch and send")).toBeInTheDocument();
    expect(screen.getAllByText("Full control")).toHaveLength(2);
    expect(screen.getByText("No mid-turn steering")).toBeInTheDocument();
    expect(screen.getByText("No steering or resume")).toBeInTheDocument();
    expect(screen.getByText("Limited control")).toBeInTheDocument();
    expect(screen.queryByText(/strongest today/i)).not.toBeInTheDocument();
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
