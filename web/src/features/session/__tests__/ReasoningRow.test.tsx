import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AgentEvent } from "@/shared/api/agents";
import { ReasoningRow, stripMarkdown, thoughtProse } from "../ReasoningRow";

describe("stripMarkdown", () => {
  it("strips bold markers, keeping the emphasized text", () => {
    expect(stripMarkdown("**Updating job commits** and pushing")).toBe(
      "Updating job commits and pushing",
    );
  });

  it("strips italics, inline code, headings, and links", () => {
    expect(stripMarkdown("_Planning_ the `deploy.sh` step")).toBe("Planning the deploy.sh step");
    expect(stripMarkdown("### Verifying deployment tasks")).toBe("Verifying deployment tasks");
    expect(stripMarkdown("See [the runbook](https://example.com/runbook) first")).toBe(
      "See the runbook first",
    );
  });

  it("leaves plain text untouched", () => {
    expect(stripMarkdown("Cross-checking the release steps")).toBe(
      "Cross-checking the release steps",
    );
  });
});

describe("thoughtProse", () => {
  it("keeps the whole thought, markdown stripped, so the clamp decides how much shows", () => {
    const prose = thoughtProse(
      "**Planning code deployment** and checking the manual-app workflow\nCross-checking the release steps against the runbook first.",
    );
    expect(prose).not.toMatch(/[*_`#]/);
    expect(prose).toContain("Cross-checking the release steps");
    expect(prose).not.toMatch(/more lines?/);
  });

  it("folds blank lines so a paragraph break costs one line, not two", () => {
    expect(thoughtProse("\n\nFirst paragraph.\n\n\nSecond paragraph.\n")).toBe("First paragraph.\nSecond paragraph.");
  });

  it("never truncates by characters", () => {
    const long = "word ".repeat(80).trim();
    expect(thoughtProse(long)).toBe(long);
  });
});

describe("ReasoningRow control", () => {
  const event = { id: 7, content_text: "Short thought." } as unknown as AgentEvent;

  afterEach(() => vi.restoreAllMocks());

  it("offers no expand control when the thought fits its three lines", () => {
    vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(40);
    vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockReturnValue(40);
    render(<ReasoningRow event={event} isSelected={false} />);
    expect(screen.getByText("Short thought.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Expand reasoning" })).toBeNull();
  });

  it("offers the expand control when the clamp cuts the thought", () => {
    vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(40);
    vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockReturnValue(120);
    render(<ReasoningRow event={event} isSelected={false} />);
    expect(screen.getByRole("button", { name: "Expand reasoning" })).toBeTruthy();
  });
});
