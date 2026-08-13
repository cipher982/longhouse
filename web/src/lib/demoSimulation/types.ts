import type { GridTimeline } from "@longhouse/video/demo";

export type DemoToolName = "Read" | "Grep" | "Edit" | "Bash";

interface DemoEventBase {
  id: string;
  t: number;
}

export type DemoSessionEvent =
  | (DemoEventBase & { type: "instruction_received"; prompt: string })
  | (DemoEventBase & { type: "assistant_text"; text: string })
  | (DemoEventBase & {
      type: "tool_started";
      callId: string;
      tool: DemoToolName;
      input: Record<string, unknown>;
      display: string;
    })
  | (DemoEventBase & {
      type: "tool_result";
      callId: string;
      tool: DemoToolName;
      output: string;
      failed?: boolean;
    })
  | (DemoEventBase & {
      type: "diff_applied";
      file: string;
      before: string;
      after: string;
      line: number;
    })
  | (DemoEventBase & {
      type: "test_result";
      callId: string;
      command: string;
      output: string;
      passed: boolean;
    })
  | (DemoEventBase & { type: "completed"; summary: string })
  | (DemoEventBase & { type: "ready" });

export interface DemoRecipe {
  id: string;
  shortLabel: string;
  prompt: string;
  file: string;
  diagnosis: readonly [string, string];
  readOutput: string;
  before: string;
  after: string;
  line: number;
  testCommand: string;
  testOutput: string;
  summary: string;
  precheckFailure?: string;
}

export interface DemoStory {
  id: string;
  ordinal: number;
  recipeId: string;
  shortLabel: string;
  prompt: string;
  file: string;
  durationSec: number;
  events: DemoSessionEvent[];
  timeline: GridTimeline;
}

export type DemoStoryPhase = "queued" | "working" | "complete";

export interface DemoStoryState {
  phase: DemoStoryPhase;
  message: string;
  detail: string;
  progress: number;
}
