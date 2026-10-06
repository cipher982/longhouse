import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import { TimelinePane } from "../TimelinePane";
import type { TimelineItem } from "@/shared/session/model";

const apiMocks = vi.hoisted(() => ({ fetchSessionEventBodies: vi.fn() }));
vi.mock("@/shared/api/agents", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/shared/api/agents")>()),
  fetchSessionEventBodies: apiMocks.fetchSessionEventBodies,
}));

const fullOutput = Array.from({ length: 40 }, (_, i) => `test ${i + 1} passed`).join("\n");
const preview = ["test 1 passed", "test 2 passed", "… 30 more lines …", ...Array.from({ length: 8 }, (_, i) => `test ${i + 33} passed`)].join("\n");

function liteToolItem(): TimelineItem {
  return {
    kind: "tool",
    interaction: {
      key: "tool:t1",
      toolName: "Bash",
      callEvent: {
        id: "e2",
        cursor: "c2",
        role: "assistant",
        content_text: null,
        tool_name: "Bash",
        tool_input_json: { command: "pytest -q" },
        tool_output_text: null,
        tool_call_id: "t1",
        tool_call_state: "completed",
        timestamp: "2026-10-06T12:00:01Z",
        in_active_context: true,
      },
      resultEvent: {
        id: "e3",
        cursor: "c3",
        role: "tool",
        content_text: null,
        tool_name: "Bash",
        tool_input_json: null,
        tool_output_text: preview,
        tool_output_truncated: true,
        tool_output_original_chars: fullOutput.length,
        lite_body: { session_id: "s-1", cursor: "c3" },
        tool_call_id: "t1",
        tool_call_state: "completed",
        timestamp: "2026-10-06T12:00:02Z",
        in_active_context: true,
      },
      pairing: "id",
      anchorId: "e2",
      timestamp: "2026-10-06T12:00:01Z",
    },
  };
}

describe("a tool row from a lite page", () => {
  it("loads its full output when expanded", async () => {
    apiMocks.fetchSessionEventBodies.mockResolvedValue({
      events: [
        {
          id: "e3",
          cursor: "c3",
          content_text: null,
          tool_name: "Bash",
          tool_input_json: null,
          tool_output_text: fullOutput,
          tool_presentation: null,
        },
      ],
      missing: [],
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <TimelinePane
          items={[liteToolItem()]}
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
        />
      </QueryClientProvider>,
    );

    // Collapsed, the row shows the server's preview and fetches nothing.
    expect(screen.getByTestId("tool-output-preview").textContent).toContain("… 30 more lines …");
    expect(apiMocks.fetchSessionEventBodies).not.toHaveBeenCalled();

    const head = screen.getByRole("button", { expanded: false });
    fireEvent.pointerEnter(head);
    expect(apiMocks.fetchSessionEventBodies).toHaveBeenCalledWith("s-1", ["c3"]);
    fireEvent.click(head);

    // Line 17 exists only in the full body, never in the preview.
    await waitFor(() => expect(document.body.textContent).toContain("test 17 passed"));
    expect(apiMocks.fetchSessionEventBodies).toHaveBeenCalledTimes(1);
  });
});
