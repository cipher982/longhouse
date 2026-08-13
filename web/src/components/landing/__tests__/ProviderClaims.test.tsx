import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { IntegrationsSection } from "../IntegrationsSection";
import { TrustSection } from "../TrustSection";

describe("landing provider claims", () => {
  it("renders readable provider rails from the capability contract", () => {
    render(
      <MemoryRouter>
        <IntegrationsSection />
      </MemoryRouter>,
    );

    expect(screen.getByText("Sync, timeline, and full-text search")).toBeInTheDocument();

    const rails = screen.getAllByRole("listitem");
    const rowNames = rails.map((rail) =>
      rail.querySelector(".landing-provider-row-name")?.textContent,
    );
    expect(rowNames).toEqual([
      "Claude Code",
      "Codex CLI",
      "Cursor Agent",
      "OpenCode",
      "Pi Agent",
      "Antigravity CLI",
    ]);

    const cellsFor = (name: string) => {
      const rail = rails.find(
        (item) => item.querySelector(".landing-provider-row-name")?.textContent === name,
      );
      return Array.from(rail!.querySelectorAll(".landing-provider-capability"))
        .map((chip) => chip.getAttribute("data-supported"));
    };

    // search, launch, interrupt, mid-turn, resume
    expect(cellsFor("Claude Code")).toEqual(["true", "true", "true", "true", "true"]);
    expect(cellsFor("Cursor Agent")).toEqual(["true", "true", "true", "false", "true"]);
    expect(cellsFor("OpenCode")).toEqual(["true", "true", "true", "false", "true"]);
    expect(cellsFor("Pi Agent")).toEqual(["true", "true", "true", "false", "false"]);
    expect(cellsFor("Antigravity CLI")).toEqual(["true", "false", "false", "false", "false"]);
    expect(screen.getByText(/Resuming a dead session is not wired up yet/i)).toBeInTheDocument();
  });

  it("renders FAQ provider answer consistent with the capability matrix", async () => {
    const user = userEvent.setup();
    render(<TrustSection />);

    await user.click(screen.getByRole("button", { name: /Which providers are strongest today\?/i }));

    // Must match the provider rails: full set only for Claude Code and Codex,
    // no mid-turn steering on Cursor/OpenCode, Pi without resume, Antigravity sync-only.
    const answer = screen.getByText(/Claude Code and Codex have the full set/i);
    expect(answer.textContent).toMatch(/Cursor Agent and OpenCode do everything except mid-turn steering/i);
    expect(answer.textContent).toMatch(/Pi Agent can launch, send, and interrupt, but resume is not wired up yet/i);
    expect(answer.textContent).toMatch(/Antigravity sessions sync into the timeline for watching and search only/i);
    expect(answer.textContent).not.toMatch(/Antigravity can launch/i);
  });
});
