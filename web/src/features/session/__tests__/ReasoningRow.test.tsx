import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
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

  it("follows the remounted text so a later overflow brings the control back", () => {
    const observed: { node: Element; fire: () => void }[] = [];
    vi.stubGlobal(
      "ResizeObserver",
      class {
        constructor(private cb: () => void) {}
        observe(node: Element) { observed.push({ node, fire: this.cb }); }
        disconnect() {}
      },
    );
    let scroll = 40;
    vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(40);
    vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockImplementation(() => scroll);
    render(<ReasoningRow event={event} isSelected={false} />);
    expect(screen.queryByRole("button", { name: "Expand reasoning" })).toBeNull();
    const current = observed.filter((entry) => entry.node.isConnected);
    expect(current.length).toBeGreaterThan(0);
    scroll = 120;
    act(() => current.forEach((entry) => entry.fire()));
    expect(screen.getByRole("button", { name: "Expand reasoning" })).toBeTruthy();
    vi.unstubAllGlobals();
  });
});

describe("live thinking row", () => {
  const live: AgentEvent = {
    id: -1_000_000_005,
    role: "assistant",
    content_text: "Thinking:\nweighing the two options",
    interaction_kind: "provider_reasoning",
    tool_name: null,
    tool_input_json: null,
    tool_output_text: null,
    tool_call_id: null,
    timestamp: "2026-03-22T22:00:05Z",
    in_active_context: true,
  } as AgentEvent;

  afterEach(() => {
    window.localStorage.clear();
    cleanup();
  });

  it("is collapsed by default as a single Thinking row", () => {
    render(<ReasoningRow event={live} isSelected={false} />);
    expect(screen.getByRole("button", { name: "Expand thinking" })).toBeTruthy();
    expect(screen.queryByText(/weighing the two options/)).toBeNull();
  });

  it("opens on click and remembers the choice for the next viewer load", () => {
    const { unmount } = render(<ReasoningRow event={live} isSelected={false} />);
    fireEvent.click(screen.getByRole("button", { name: "Expand thinking" }));
    expect(screen.getByText(/weighing the two options/)).toBeTruthy();
    expect(window.localStorage.getItem("longhouse.liveThinking.expanded")).toBe("1");
    unmount();
    render(<ReasoningRow event={live} isSelected={false} />);
    expect(screen.getByText(/weighing the two options/)).toBeTruthy();
  });

  it("toggles on the t key, and ignores it in a field or with a modifier", () => {
    render(
      <>
        <input data-testid="field" />
        <ReasoningRow event={live} isSelected={false} />
      </>,
    );
    fireEvent.keyDown(screen.getByTestId("field"), { key: "t" });
    expect(screen.queryByText(/weighing the two options/)).toBeNull();
    fireEvent.keyDown(window, { key: "t", ctrlKey: true });
    expect(screen.queryByText(/weighing the two options/)).toBeNull();
    fireEvent.keyDown(window, { key: "t" });
    expect(screen.getByText(/weighing the two options/)).toBeTruthy();
    fireEvent.keyDown(window, { key: "t" });
    expect(screen.queryByText(/weighing the two options/)).toBeNull();
  });

  it("starts collapsed when storage throws", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    render(<ReasoningRow event={live} isSelected={false} />);
    expect(screen.getByRole("button", { name: "Expand thinking" })).toBeTruthy();
    vi.restoreAllMocks();
  });
});
