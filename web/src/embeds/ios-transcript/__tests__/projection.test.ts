import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentEvent, AgentSessionProjectionItem } from "@/shared/api/agents";
import { renderTranscript, resetTranscriptStateForTesting } from "../render";
import type { OverlayRow, TranscriptPayload } from "../types";

function ev(partial: Partial<AgentEvent> & { id: number; role: string }): AgentSessionProjectionItem {
  const event = {
    content_text: null,
    tool_input_json: null,
    tool_output_text: null,
    tool_call_id: null,
    timestamp: `2026-11-15T10:00:${String(partial.id).padStart(2, "0")}Z`,
    in_active_context: true,
    ...partial,
  } as AgentEvent;
  return { kind: "event", event, timestamp: event.timestamp } as unknown as AgentSessionProjectionItem;
}

let sequence = 0;
function render(payload: TranscriptPayload) {
  sequence += 1;
  const bytes = new TextEncoder().encode(JSON.stringify(payload));
  const base64 = btoa(Array.from(bytes, (byte) => String.fromCharCode(byte)).join(""));
  return renderTranscript(base64, true, sequence, "snapshot");
}

const root = () => document.getElementById("root")!;

beforeEach(() => {
  document.body.innerHTML = '<main id="root"></main>';
  resetTranscriptStateForTesting();
  vi.spyOn(window, "scrollTo").mockImplementation(() => {});
});

const narrated = [
  ev({ id: 1, role: "user", content_text: "check the build" }),
  ev({ id: 2, role: "assistant", content_text: "Looking at the CI log first.", message_phase: "commentary" }),
  ev({ id: 3, role: "assistant", tool_name: "Bash", tool_call_id: "a", tool_call_state: "completed",
    tool_input_json: { command: "gh run list", description: "List recent CI runs" } }),
  ev({ id: 4, role: "tool", tool_call_id: "a", tool_output_text: "ok", tool_call_state: "completed" }),
  ev({ id: 5, role: "assistant", content_text: "The build is green.", message_phase: "final_answer" }),
];

describe("renderTranscript with a served projection", () => {
  it("builds rows in the document and reports the last one", () => {
    const metrics = render({ projection: { items: narrated } });
    expect(metrics.row_count).toBe(4);
    expect(metrics.latest_item_id).toBe("prose:5");
    expect(root().querySelectorAll(".row")).toHaveLength(4);
  });

  it("renders the steps under commentary as dim trailing lines, with no commentary chip", () => {
    render({ projection: { items: narrated } });
    const trail = root().querySelectorAll("details.trail");
    expect(trail).toHaveLength(1);
    expect(trail[0].querySelector(".tool-subtitle")?.textContent).toBe("List recent CI runs");
    expect(trail[0].querySelector(".tool-title")).toBeNull();
    expect(root().querySelector(".phase-badge")).toBeNull();
    const finalAnswer = root().querySelector(".message.assistant:not(.commentary)");
    expect(finalAnswer?.textContent).toContain("The build is green.");
  });

  it("places optimistic rows by send time and merges wake notices after placing", () => {
    const overlay: OverlayRow[] = [
      { row: { id: "input-1", kind: "submitted", role: "user", body: "late", status: "sent" }, placedAtSendTime: false, createdAtMs: 0, phase: "sent" },
      { row: { id: "input-2", kind: "submitted", role: "user", body: "mid", status: "sent" }, placedAtSendTime: true, createdAtMs: Date.parse("2026-11-15T10:00:02.500Z"), phase: "sent" },
    ];
    render({ projection: { items: narrated, overlay } });
    const ids = Array.from(root().children).map((node) => node.textContent?.trim().slice(0, 12));
    expect(root().querySelectorAll(".submitted")).toHaveLength(2);
    const submitted = Array.from(root().children).filter((n) => n.classList.contains("submitted"));
    expect(submitted[0].textContent).toContain("mid");
    expect(root().lastElementChild?.textContent).toContain("late");
    expect(ids.length).toBe(6);
    // "mid" stands before the tool row (10:00:03), after the commentary (10:00:02).
    const order = Array.from(root().children).map((n) => n.className.split(" ").slice(0, 3).join("."));
    expect(order.indexOf("row.message.user")).toBeLessThan(order.findIndex((c) => c.startsWith("tool.row")));
  });
});
