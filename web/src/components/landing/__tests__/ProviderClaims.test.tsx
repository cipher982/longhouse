import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { IntegrationsSection } from "../IntegrationsSection";
import { TrustSection } from "../TrustSection";

describe("landing provider claims", () => {
  it("renders the capability matrix with plain supported/unsupported cells", () => {
    render(
      <MemoryRouter>
        <IntegrationsSection />
      </MemoryRouter>,
    );

    const table = screen.getByRole("table");
    const headers = within(table)
      .getAllByRole("columnheader")
      .map((th) => th.textContent);
    expect(headers).toEqual([
      "Provider",
      "Sync & search",
      "Launch & send",
      "Interrupt",
      "Steer mid-turn",
      "Resume",
    ]);

    const rowNames = Array.from(table.querySelectorAll("tbody th")).map(
      (th) => th.textContent,
    );
    expect(rowNames).toEqual([
      "Claude Code",
      "Codex CLI",
      "Cursor Agent",
      "OpenCode",
      "Antigravity CLI",
    ]);

    const cellsFor = (name: string) =>
      within(within(table).getByText(name).closest("tr")!)
        .getAllByRole("cell")
        .map((td) => (td.className.includes("yes") ? "yes" : "no"));

    // sync, launch & send, interrupt, steer, resume
    expect(cellsFor("Claude Code")).toEqual(["yes", "yes", "yes", "yes", "yes"]);
    expect(cellsFor("Cursor Agent")).toEqual(["yes", "no", "no", "no", "no"]);
    expect(cellsFor("OpenCode")).toEqual(["yes", "yes", "yes", "no", "no"]);
    expect(cellsFor("Antigravity CLI")).toEqual(["yes", "no", "no", "no", "no"]);
  });

  it("renders FAQ provider answer consistent with the capability matrix", async () => {
    const user = userEvent.setup();
    render(<TrustSection />);

    await user.click(screen.getByRole("button", { name: /Which providers are strongest today\?/i }));

    expect(
      screen.getByText(/Claude Code, Codex, and OpenCode have native managed control paths/i),
    ).toBeInTheDocument();
  });
});
