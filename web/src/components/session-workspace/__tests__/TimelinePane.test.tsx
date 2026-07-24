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

function makeActivityGroupItem(): TimelineItem {
  const toolItem = makeToolWithMediaItem();
  if (toolItem.kind !== "tool") throw new Error("fixture must be a tool");
  const first = toolItem.interaction;
  return {
    kind: "activity_group",
    group: {
      key: "activity:6",
      interactions: [
        first,
        { ...first, key: "id:second", anchorId: 8, toolName: "Edit" },
      ],
      timestamp: first.timestamp,
      anchorId: first.anchorId,
    },
  };
}

function makeNamedShellActivityGroupItem(): TimelineItem {
  const toolItem = makeToolWithMediaItem();
  if (toolItem.kind !== "tool") throw new Error("fixture must be a tool");
  const namedInteraction = (key: string, anchorId: number) => ({
    ...toolItem.interaction,
    key,
    anchorId,
    callEvent: toolItem.interaction.callEvent ? {
      ...toolItem.interaction.callEvent,
      id: anchorId,
      tool_name: "exec_command",
      tool_input_json: { cmd: `gh run view ${anchorId}` },
      tool_presentation: {
        version: 2,
        disposition: "direct",
        tool_name: "exec_command",
        source_tool_name: "exec_command",
        execution_method: null,
        label: "Shell",
        icon: "$",
        color: "warning",
        tier: "action",
        aggregate: null,
        mcp_namespace: null,
        tool_input_json: { cmd: `gh run view ${anchorId}` },
        rule_id: "fixture",
        wrapper_recedes: false,
        children: [],
        shell_summary: {
          version: 1,
          confidence: "syntactic",
          operations: [{ key: "gh run view", label: "gh run view", executable: "gh", subcommands: ["run", "view"], count: 1 }],
          candidate_count: 1,
          truncated: false,
          dynamic: false,
          parse_error: null,
          parser_id: "fixture",
          shape_registry_version: 1,
        },
      },
    } : null,
    presentation: toolItem.interaction.callEvent?.tool_presentation,
    toolName: "exec_command",
  });
  const first = namedInteraction("id:first", 6);
  const second = namedInteraction("id:second", 8);
  first.presentation = first.callEvent?.tool_presentation;
  second.presentation = second.callEvent?.tool_presentation;
  return {
    kind: "activity_group",
    group: {
      key: "activity:6",
      interactions: [first, second],
      timestamp: first.timestamp,
      anchorId: first.anchorId,
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


/** Render the pane with default props; only `items` varies in these tests. */
function renderPane(items: TimelineItem[]) {
  return render(
    <TimelinePane
      items={items}
      totalEntries={items.length}
      loadedEntries={items.length}
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
}

/** Edit interaction with a real `old_string`/`new_string` shape. */
function makeEditItem(
  key: string,
  filePath: string,
  oldStr: string,
  newStr: string,
): TimelineItem {
  return {
    kind: "tool",
    interaction: {
      key,
      toolName: "Edit",
      callEvent: {
        id: 20,
        role: "assistant",
        content_text: null,
        tool_name: "Edit",
        tool_input_json: { file_path: filePath, old_string: oldStr, new_string: newStr },
        tool_output_text: null,
        tool_call_id: key,
        tool_call_state: "completed",
        timestamp: "2026-03-19T16:49:00Z",
        in_active_context: true,
      },
      resultEvent: {
        id: 21,
        role: "tool",
        content_text: null,
        tool_name: "Edit",
        tool_input_json: null,
        tool_output_text: "ok",
        tool_call_id: key,
        timestamp: "2026-03-19T16:49:01Z",
        in_active_context: true,
      },
      pairing: "id",
      anchorId: 20,
      timestamp: "2026-03-19T16:49:00Z",
    },
  } as TimelineItem;
}

/**
 * Failed shell call. `wrapped` selects the Longhouse wrapper text format
 * (parsed exit code) versus a structured JSON failure with no parsed exit.
 */
function makeFailedToolItem(output: string, wrapped = true): TimelineItem {
  return {
    kind: "tool",
    interaction: {
      key: "id:failed",
      toolName: "Bash",
      callEvent: {
        id: 30,
        role: "assistant",
        content_text: null,
        tool_name: "Bash",
        tool_input_json: { command: "make test" },
        tool_output_text: null,
        tool_call_id: "failed",
        tool_call_state: "completed",
        timestamp: "2026-03-19T16:49:00Z",
        in_active_context: true,
      },
      resultEvent: {
        id: 31,
        role: "tool",
        content_text: null,
        tool_name: "Bash",
        tool_input_json: null,
        tool_output_text: wrapped
          ? `Wall time: 1.0 seconds\nProcess exited with code 2\nOutput:\n${output}`
          : JSON.stringify({ ok: false, output }),
        tool_call_id: "failed",
        timestamp: "2026-03-19T16:49:05Z",
        in_active_context: true,
      },
      pairing: "id",
      anchorId: 30,
      timestamp: "2026-03-19T16:49:00Z",
    },
  } as TimelineItem;
}

describe("TimelinePane", () => {
  it("keeps repeated shell work recognizable while collapsed", () => {
    render(
      <TimelinePane
        items={[makeNamedShellActivityGroupItem()]}
        totalEntries={4}
        loadedEntries={4}
        abandonedEvents={0}
        showAbandonedBranches={false}
        onShowAbandonedBranchesChange={vi.fn()}
        hasPreviousPage={false}
        isFetchingPreviousPage={false}
        onFetchPreviousPage={vi.fn()}
        selectedKey={null}
        onSelectKey={vi.fn()}
      />,
    );

    expect(screen.getByText("Ran gh run view ×2")).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();
  });

  it("reveals the exact child when a grouped tool is selected", () => {
    render(
      <TimelinePane
        items={[makeActivityGroupItem()]}
        totalEntries={4}
        loadedEntries={4}
        abandonedEvents={0}
        showAbandonedBranches={false}
        onShowAbandonedBranchesChange={vi.fn()}
        hasPreviousPage={false}
        isFetchingPreviousPage={false}
        onFetchPreviousPage={vi.fn()}
        selectedKey="tool:id:second"
        onSelectKey={vi.fn()}
      />,
    );

    expect(screen.getByText("Edit")).toBeInTheDocument();
    expect(screen.getByText(/^input$/i)).toBeInTheDocument();
  });

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

  it("shows the file and diff stat on a collapsed edit row", () => {
    const item = makeEditItem("id:edit-1", "src/deep/nested/timelineModel.ts", "a\nb\nc", "a\nB\nc");
    renderPane([item]);
    // Basename only in the header; the full path lives in the expanded diff.
    expect(screen.getByTestId("tool-edit-stat").textContent).toBe("timelineModel.ts +1 −1");
  });

  it("renders a bounded failure preview without a click", () => {
    const lines = Array.from({ length: 40 }, (_, i) => `line ${i}`).join("\n");
    renderPane([makeFailedToolItem(lines)]);
    const preview = screen.getByTestId("tool-failure-preview");
    // Head is kept as well as tail: a stack trace's heading must survive.
    expect(preview.textContent).toContain("line 0");
    expect(preview.textContent).toContain("line 39");
    expect(preview.textContent).toContain("more lines");
    expect(preview.textContent).not.toContain("line 20");
  });

  it("marks a structured failure as failed even without a parsed exit code", () => {
    renderPane([makeFailedToolItem("boom", false)]);
    const row = screen.getAllByTestId("session-timeline-row")[0];
    expect(row.getAttribute("data-status")).toBe("error");
  });

  it("keeps two grouped children open at once", () => {
    const a = makeEditItem("id:edit-a", "a.ts", "x", "y");
    const b = makeEditItem("id:edit-b", "b.ts", "x", "y");
    if (a.kind !== "tool" || b.kind !== "tool") throw new Error("fixture");
    const group: TimelineItem = {
      kind: "activity_group",
      group: {
        key: "activity:20",
        interactions: [a.interaction, b.interaction],
        timestamp: a.interaction.timestamp,
        anchorId: a.interaction.anchorId,
      },
    };
    const { container } = renderPane([group]);
    fireEvent.click(screen.getByText(/Edited/));

    const heads = container.querySelectorAll(".tl-noise__item-head");
    expect(heads.length).toBe(2);
    fireEvent.click(heads[0]);
    fireEvent.click(heads[1]);

    // Previously an accordion: opening the second closed the first.
    expect(container.querySelectorAll(".tl-noise__item.is-expanded").length).toBe(2);
    expect(container.querySelectorAll(".tl-diff").length).toBe(2);
  });
});
