import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { TimelineItem } from "../../../../lib/sessionWorkspace";
import { PhoneSessionScreen } from "../PhoneSessionScreen";

function makeItems(): TimelineItem[] {
  return [
    {
      kind: "tool",
      interaction: {
        key: "id:t1",
        toolName: "Bash",
        callEvent: {
          id: 1,
          role: "assistant",
          content_text: null,
          tool_name: "Bash",
          tool_input_json: { command: "python3 test_inventory.py" },
          tool_output_text: null,
          tool_call_id: "t1",
          timestamp: "2026-08-12T20:00:00Z",
        },
        resultEvent: {
          id: 2,
          role: "tool",
          content_text: null,
          tool_name: "Bash",
          tool_input_json: null,
          tool_output_text: "all tests passed\n",
          tool_call_id: "t1",
          timestamp: "2026-08-12T20:00:01Z",
        },
        pairing: "id",
        anchorId: 1,
        timestamp: "2026-08-12T20:00:00Z",
      },
    },
    {
      kind: "message",
      event: {
        id: 3,
        role: "assistant",
        content_text: "Fixed it. All tests pass.",
        tool_name: null,
        tool_input_json: null,
        tool_output_text: null,
        tool_call_id: null,
        timestamp: "2026-08-12T20:00:02Z",
      },
    },
  ];
}

const baseProps = {
  title: "Live demo repo",
  composerText: "",
  sent: false,
  working: false,
  onSend: () => {},
};

describe("PhoneSessionScreen", () => {
  it("renders canonical tool rows and assistant prose", () => {
    render(
      <PhoneSessionScreen
        {...baseProps}
        transcript={{ sentMessage: "go", items: makeItems() }}
      />,
    );

    expect(screen.getByText("Bash")).toBeInTheDocument();
    expect(screen.getByText("python3 test_inventory.py")).toBeInTheDocument();
    expect(screen.getByText("all tests passed")).toBeInTheDocument();
    expect(screen.getByText("Fixed it. All tests pass.")).toBeInTheDocument();
  });

  it("disables the composer once a message is sent", () => {
    render(
      <PhoneSessionScreen
        {...baseProps}
        sent
        working
        composerDisabled
        transcript={{ sentMessage: "go", items: [] }}
      />,
    );

    const input = screen.getByRole("textbox", { name: "Message to live session" });
    expect(input).toBeDisabled();
    expect(input).toHaveValue("");
    expect(screen.getByText("go")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Message sent" })).toBeDisabled();
  });

  it("clears the composer once the instruction is submitted", () => {
    // The composer is controlled: after send, LiveDemo sets composerText to "".
    // The screen must reflect that empty value, not re-arm the send button.
    render(
      <PhoneSessionScreen
        {...baseProps}
        sent
        composerText=""
        transcript={{ sentMessage: "Fix the bug", items: [] }}
      />,
    );

    expect(screen.getByRole("textbox", { name: "Message to live session" })).toHaveValue("");
    expect(screen.getByRole("button", { name: "Message sent" })).toBeDisabled();
  });
});
