import { describe, expect, it } from "vitest";
import { projectLiveSessionEvents, type LiveSessionEvent } from "../liveSessionEvents";

describe("projectLiveSessionEvents", () => {
  it("pairs live tool results and preserves Claude's assistant text", () => {
    const events: LiveSessionEvent[] = [
      { id: "user", role: "user", content_text: "Fix it" },
      {
        id: "edit",
        role: "assistant",
        tool_name: "Edit",
        tool_input_json: { file_path: "/demo-repo/inventory.py" },
        tool_call_id: "edit-1",
      },
      {
        id: "edit-result",
        role: "tool",
        tool_call_id: "edit-1",
        tool_output_text: "Updated inventory.py",
      },
      {
        id: "bash",
        role: "assistant",
        tool_name: "Bash",
        tool_input_json: { command: "python3 test_inventory.py\necho ignored" },
        tool_call_id: "bash-1",
      },
      {
        id: "bash-result",
        role: "tool",
        tool_call_id: "bash-1",
        tool_output_text: "all tests passed\n",
      },
      { id: "answer", role: "assistant", content_text: "Fixed it. All tests pass." },
    ];

    expect(projectLiveSessionEvents(events)).toEqual([
      {
        id: "edit",
        tSec: 1,
        kind: "tool",
        title: "Edit",
        subtitle: "inventory.py",
        result: "Updated inventory.py",
      },
      {
        id: "bash",
        tSec: 3,
        kind: "tool",
        title: "Bash",
        subtitle: "python3 test_inventory.py",
        result: "all tests passed",
      },
      {
        id: "answer",
        tSec: 5,
        kind: "assistant",
        title: "Fixed it. All tests pass.",
      },
    ]);
  });
});
