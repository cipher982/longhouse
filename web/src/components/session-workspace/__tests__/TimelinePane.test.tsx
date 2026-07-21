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

const mediaMessageItem: TimelineItem = {
  kind: "message",
  event: {
    ...messageItem.event,
    id: 5,
    content_text: "Here is the screenshot.",
    media_refs: [
      {
        sha256: "abc123def456abc123def456abc123def456abc123def456abc123def456abcd",
        media_state: "present",
        mime_type: "image/png",
        byte_size: 1024,
        blob_url: "/api/media/abc123/blob",
        thumb_url: "/api/media/abc123/thumb",
        original_kind: "data_url_backfill",
      },
    ],
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

function makeToolWithMediaItem(): TimelineItem {
  return {
    kind: "tool",
    interaction: {
      key: "id:tool-media",
      toolName: "Bash",
      callEvent: {
        id: 6,
        role: "assistant",
        content_text: null,
        tool_name: "Bash",
        tool_input_json: { command: "capture" },
        tool_output_text: null,
        tool_call_id: "tool-media",
        tool_call_state: "completed",
        timestamp: "2026-03-19T16:49:00Z",
        in_active_context: true,
      },
      resultEvent: {
        id: 7,
        role: "tool",
        content_text: null,
        tool_name: "Bash",
        tool_input_json: null,
        tool_output_text: "saved screenshot",
        tool_call_id: "tool-media",
        timestamp: "2026-03-19T16:49:03Z",
        in_active_context: true,
        media_refs: [
          {
            sha256: "def456abc123def456abc123def456abc123def456abc123def456abc123def4",
            media_state: "present",
            mime_type: "image/jpeg",
            byte_size: 2048,
            blob_url: "/api/media/def456/blob",
            thumb_url: null,
            original_kind: "data_url_backfill",
          },
        ],
      },
      pairing: "id",
      anchorId: 6,
      timestamp: "2026-03-19T16:49:00Z",
    },
  };
}

function makeAskUserQuestionItem(state: "running" | "dropped" = "dropped"): TimelineItem {
  return {
    kind: "tool",
    interaction: {
      key: "id:toolu-question",
      toolName: "AskUserQuestion",
      callEvent: {
        id: 4,
        role: "assistant",
        content_text: null,
        tool_name: "AskUserQuestion",
        tool_input_json: {
          questions: [
            {
              id: "image_scope",
              header: "Image scope",
              question: "How should I run the full image download?",
              options: [
                {
                  label: "ibsrv first, then external",
                  description: "Download MBWorld-hosted images first.",
                },
                {
                  label: "Both back-to-back",
                  description: "Queue both image sets in one run.",
                },
              ],
            },
          ],
        },
        tool_output_text: null,
        tool_call_id: "toolu-question",
        tool_call_state: state,
        timestamp: "2026-03-19T16:48:00Z",
        in_active_context: true,
      },
      resultEvent: null,
      pairing: "id",
      anchorId: 4,
      timestamp: "2026-03-19T16:48:00Z",
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

  it("renders present media refs on message rows", () => {
    render(
      <TimelinePane
        items={[mediaMessageItem]}
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

    const image = screen.getByAltText("Session media abc123def456");
    expect(image).toHaveAttribute("src", "/api/media/abc123/thumb");
    expect(image.closest("a")).toHaveAttribute("href", "/api/media/abc123/blob");
  });

  it("suppresses media refs when rendering shared read-only timelines", () => {
    render(
      <TimelinePane
        items={[mediaMessageItem]}
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
        renderMedia={false}
      />,
    );

    expect(screen.queryByTestId("session-event-media")).not.toBeInTheDocument();
    expect(screen.queryByAltText("Session media abc123def456")).not.toBeInTheDocument();
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

  it("renders provider-native free-form tool input without crashing the timeline", () => {
    const item = makePendingToolItem("running");
    if (item.kind !== "tool") throw new Error("expected tool fixture");
    item.interaction.callEvent!.tool_input_json = "*** Begin Patch\n*** Update File: example.ts";

    render(
      <TimelinePane
        items={[item]}
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
    expect(row).toHaveTextContent(
      "*** Begin Patch *** Update File: example.ts",
    );
    fireEvent.click(screen.getByRole("button", { name: /Bash/ }));
    expect(row).toHaveTextContent("*** Begin Patch");
    expect(row).toHaveTextContent("*** Update File: example.ts");
  });

  it("renders AskUserQuestion as a readable terminal-only question instead of a dropped tool", () => {
    render(
      <TimelinePane
        items={[makeAskUserQuestionItem("dropped")]}
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

    const row = screen.getByTestId("session-question-row");
    expect(row).toHaveAttribute("data-status", "waiting");
    expect(row).toHaveTextContent("Needs answer");
    expect(row).toHaveTextContent("Image scope");
    expect(row).toHaveTextContent("How should I run the full image download?");
    expect(row).toHaveTextContent("ibsrv first, then external");
    expect(row).toHaveTextContent("Both back-to-back");
    expect(row).toHaveTextContent("Answer this in the terminal.");
    expect(row).not.toHaveTextContent("dropped");
    expect(row).not.toHaveTextContent("running");
  });

  it("renders tool result media inside expanded tool details", () => {
    render(
      <TimelinePane
        items={[makeToolWithMediaItem()]}
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

    fireEvent.click(screen.getByRole("button", { name: /Bash/ }));
    const image = screen.getByAltText("Session media def456abc123");
    expect(image).toHaveAttribute("src", "/api/media/def456/blob");
    expect(screen.getByTestId("session-event-media")).toBeInTheDocument();
  });
});
