import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { CommisToolCard } from "../CommisToolCard";
import type { OikosToolCall } from "../../../lib/oikos-tool-store";

describe("CommisToolCard", () => {
  it("uses cloud-session wording in the visible card header", () => {
    const tool: OikosToolCall = {
      toolCallId: "tool-1",
      toolName: "spawn_commis",
      status: "completed",
      runId: 7,
      startedAt: Date.now() - 5_000,
      completedAt: Date.now(),
      durationMs: 5_000,
      args: { task: "Inspect the deploy logs and summarize" },
      result: {
        commisStatus: "complete",
        commisSummary: "Deployment looks healthy",
        nestedTools: [],
      },
      logs: [],
    };

    render(<CommisToolCard tool={tool} />);

    expect(screen.getByText("Cloud session")).toBeInTheDocument();
    expect(screen.queryByText("Commis")).not.toBeInTheDocument();
    expect(screen.getByText("Inspect the deploy logs and summarize")).toBeInTheDocument();
  });
});
