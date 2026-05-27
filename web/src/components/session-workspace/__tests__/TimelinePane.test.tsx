import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { TimelinePane } from "../TimelinePane";
import type { TimelineItem } from "../../../lib/sessionWorkspace";

const seamItem: TimelineItem = {
  kind: "seam",
  seam: {
    key: "seam:session-cloud:2026-03-19T16:45:00Z",
    sessionId: "session-cloud",
    label: "Continuation begins",
    description: "Synced Cinder history above. New continuation messages below.",
    timestamp: "2026-03-19T16:45:00Z",
  },
};

const messageItem: TimelineItem = {
  kind: "message",
  event: {
    id: 1,
    role: "assistant",
    content_text: "Paris",
    tool_name: null,
    tool_input_json: null,
    tool_output_text: null,
    tool_call_id: null,
    timestamp: "2026-03-19T16:46:00Z",
    in_active_context: true,
  },
};

const longhouseMessageItem: TimelineItem = {
  kind: "message",
  event: {
    ...messageItem.event,
    id: 3,
    role: "user",
    content_text: "Sent from the browser",
    input_origin: {
      authored_via: "longhouse",
      session_input_id: 44,
      client_request_id: "web-origin-1",
    },
  },
};

function makeMessageItem(content: string): TimelineItem {
  return {
    kind: "message",
    event: {
      ...messageItem.event,
      content_text: content,
    },
  };
}

function makePendingToolItem(state: "running" | "dropped"): TimelineItem {
  return {
    kind: "tool",
    interaction: {
      key: "tool:pending",
      toolName: "Bash",
      callEvent: {
        id: 2,
        role: "assistant",
        content_text: null,
        tool_name: "Bash",
        tool_input_json: { command: "sleep 10; make dogfood-check" },
        tool_output_text: null,
        tool_call_id: "tool-pending",
        tool_call_state: state,
        timestamp: "2026-03-19T16:47:00Z",
        in_active_context: true,
      },
      resultEvent: null,
      pairing: "pending",
      anchorId: 2,
      timestamp: "2026-03-19T16:47:00Z",
    },
  };
}

describe("TimelinePane", () => {
  it("renders seam items inline in the timeline list", () => {
    render(
      <TimelinePane
        items={[seamItem, messageItem]}
        totalEntries={2}
        loadedEntries={2}
        abandonedEvents={0}
        showAbandonedBranches={false}
        onShowAbandonedBranchesChange={vi.fn()}
        hasPreviousPage={false}
        isFetchingPreviousPage={false}
        onFetchPreviousPage={vi.fn()}
        loading={false}
        error={null}
        selectedKey={null}
        onSelectKey={vi.fn()}
      />,
    );

    expect(screen.getByTestId("session-timeline-seam")).toBeInTheDocument();
    expect(screen.getByText("Continuation begins")).toBeInTheDocument();
    expect(screen.getByText("Synced Cinder history above. New continuation messages below.")).toBeInTheDocument();
    expect(screen.getByText("Paris")).toBeInTheDocument();
  });

  it("does not collapse normal multi-paragraph assistant prose", () => {
    const paragraphs = Array.from({ length: 80 }, (_, index) => `Paragraph ${index + 1}: normal response text.`).join("\n\n");

    render(
      <TimelinePane
        items={[makeMessageItem(paragraphs)]}
        totalEntries={1}
        loadedEntries={1}
        abandonedEvents={0}
        showAbandonedBranches={false}
        onShowAbandonedBranchesChange={vi.fn()}
        hasPreviousPage={false}
        isFetchingPreviousPage={false}
        onFetchPreviousPage={vi.fn()}
        loading={false}
        error={null}
        selectedKey={null}
        onSelectKey={vi.fn()}
      />,
    );

    expect(screen.queryByRole("button", { name: "Show full message" })).not.toBeInTheDocument();
    expect(screen.getByText("Paragraph 80: normal response text.")).toBeInTheDocument();
  });

  it("collapses only very large message dumps with a head and tail preview", () => {
    const hugeMessage = Array.from({ length: 700 }, (_, index) => `Dump line ${index + 1}`).join("\n");

    render(
      <TimelinePane
        items={[makeMessageItem(hugeMessage)]}
        totalEntries={1}
        loadedEntries={1}
        abandonedEvents={0}
        showAbandonedBranches={false}
        onShowAbandonedBranchesChange={vi.fn()}
        hasPreviousPage={false}
        isFetchingPreviousPage={false}
        onFetchPreviousPage={vi.fn()}
        loading={false}
        error={null}
        selectedKey={null}
        onSelectKey={vi.fn()}
      />,
    );

    const timeline = screen.getByTestId("session-timeline-list");
    expect(timeline).toHaveTextContent("Dump line 1");
    expect(timeline).toHaveTextContent("Dump line 700");
    expect(timeline).toHaveTextContent("... 400 lines hidden ...");
    expect(timeline).not.toHaveTextContent("Dump line 350");
    const expand = screen.getByRole("button", { name: "Show full message" });
    fireEvent.click(expand);
    expect(timeline).toHaveTextContent("Dump line 350");
    expect(timeline).not.toHaveTextContent("... 400 lines hidden ...");
    expect(screen.getByRole("button", { name: "Collapse message" })).toBeInTheDocument();
  });

  it("renders a semantic Longhouse marker for Longhouse-authored user input", () => {
    render(
      <TimelinePane
        items={[longhouseMessageItem]}
        totalEntries={1}
        loadedEntries={1}
        abandonedEvents={0}
        showAbandonedBranches={false}
        onShowAbandonedBranchesChange={vi.fn()}
        hasPreviousPage={false}
        isFetchingPreviousPage={false}
        onFetchPreviousPage={vi.fn()}
        loading={false}
        error={null}
        selectedKey={null}
        onSelectKey={vi.fn()}
      />,
    );

    expect(screen.getByTestId("session-input-origin-longhouse")).toHaveTextContent("Longhouse");
    expect(screen.getByLabelText("Sent via Longhouse")).toBeInTheDocument();
  });

  it("marks server-running tool calls as pending rows", () => {
    render(
      <TimelinePane
        items={[makePendingToolItem("running")]}
        totalEntries={1}
        loadedEntries={1}
        abandonedEvents={0}
        showAbandonedBranches={false}
        onShowAbandonedBranchesChange={vi.fn()}
        hasPreviousPage={false}
        isFetchingPreviousPage={false}
        onFetchPreviousPage={vi.fn()}
        loading={false}
        error={null}
        selectedKey={null}
        onSelectKey={vi.fn()}
      />,
    );

    const row = screen.getByTestId("session-timeline-row");
    expect(row).toHaveAttribute("data-status", "pending");
    expect(row).toHaveClass("tl-action--pending");
  });

  it("marks server-dropped tool calls as dropped/error rows", () => {
    render(
      <TimelinePane
        items={[makePendingToolItem("dropped")]}
        totalEntries={1}
        loadedEntries={1}
        abandonedEvents={0}
        showAbandonedBranches={false}
        onShowAbandonedBranchesChange={vi.fn()}
        hasPreviousPage={false}
        isFetchingPreviousPage={false}
        onFetchPreviousPage={vi.fn()}
        loading={false}
        error={null}
        selectedKey={null}
        onSelectKey={vi.fn()}
      />,
    );

    const row = screen.getByTestId("session-timeline-row");
    expect(row).toHaveAttribute("data-status", "error");
    expect(row).toHaveClass("tl-action--dropped");
  });
});
