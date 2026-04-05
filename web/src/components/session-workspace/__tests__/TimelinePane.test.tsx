import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { TimelinePane } from "../TimelinePane";
import type { TimelineItem } from "../../../lib/sessionWorkspace";

const seamItem: TimelineItem = {
  kind: "seam",
  seam: {
    key: "seam:session-cloud:2026-03-19T16:45:00Z",
    sessionId: "session-cloud",
    label: "Cloud branch begins",
    description: "Synced Cinder history above. New cloud-branch messages below.",
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
    expect(screen.getByText("Cloud branch begins")).toBeInTheDocument();
    expect(screen.getByText("Synced Cinder history above. New cloud-branch messages below.")).toBeInTheDocument();
    expect(screen.getByText("Paris")).toBeInTheDocument();
  });
});
