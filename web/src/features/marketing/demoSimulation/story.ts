import { DEMO_RECIPES } from "./recipes";
import { demoHash, demoRecipeIndex } from "./seed";
import { renderDemoTerminal } from "./terminal";
import type {
  DemoRecipe,
  DemoSessionEvent,
  DemoStory,
  DemoStoryState,
  DemoToolName,
} from "./types";

const DURATION_SEC = 5.6;

function buildEvents(seed: string, ordinal: number, recipe: DemoRecipe): DemoSessionEvent[] {
  const events: DemoSessionEvent[] = [];
  let eventNumber = 0;
  const eventId = (kind: string) => `${recipe.id}-${ordinal}-${kind}-${++eventNumber}`;
  const tool = (
    t: number,
    name: DemoToolName,
    display: string,
    input: Record<string, unknown>,
    output: string,
    failed = false,
  ) => {
    const callId = `${recipe.id}-${ordinal}-${name.toLowerCase()}-${eventNumber + 1}`;
    events.push({ type: "tool_started", id: eventId("call"), t, callId, tool: name, input, display });
    events.push({ type: "tool_result", id: eventId("result"), t: t + 0.3, callId, tool: name, output, failed });
    return callId;
  };

  events.push({ type: "instruction_received", id: eventId("prompt"), t: 0.25, prompt: recipe.prompt });

  if (recipe.precheckFailure) {
    events.push({ type: "assistant_text", id: eventId("prose"), t: 0.55, text: "I'll reproduce the failure first." });
    const failedCall = tool(0.8, "Bash", recipe.testCommand, { command: recipe.testCommand }, recipe.precheckFailure, true);
    events.push({
      type: "test_result",
      id: eventId("test"),
      t: 1.2,
      callId: failedCall,
      command: recipe.testCommand,
      output: recipe.precheckFailure,
      passed: false,
    });
    events.push({
      type: "assistant_text",
      id: eventId("diagnosis"),
      t: 1.5,
      text: recipe.diagnosis[demoHash(seed, ordinal, "diagnosis") % recipe.diagnosis.length],
    });
    tool(1.85, "Read", recipe.file, { file_path: `/demo-repo/${recipe.file}` }, recipe.readOutput);
    tool(2.5, "Edit", recipe.file, {
      file_path: `/demo-repo/${recipe.file}`,
      old_string: recipe.before,
      new_string: recipe.after,
    }, `Updated ${recipe.file}`);
    events.push({
      type: "diff_applied",
      id: eventId("diff"),
      t: 2.9,
      file: recipe.file,
      before: recipe.before,
      after: recipe.after,
      line: recipe.line,
    });
    const passedCall = tool(3.25, "Bash", recipe.testCommand, { command: recipe.testCommand }, recipe.testOutput);
    events.push({
      type: "test_result",
      id: eventId("test"),
      t: 3.65,
      callId: passedCall,
      command: recipe.testCommand,
      output: recipe.testOutput,
      passed: true,
    });
  } else {
    events.push({ type: "assistant_text", id: eventId("prose"), t: 0.55, text: "I'll inspect the failing path and run its focused tests." });
    tool(0.85, "Read", recipe.file, { file_path: `/demo-repo/${recipe.file}` }, recipe.readOutput);
    events.push({
      type: "assistant_text",
      id: eventId("diagnosis"),
      t: 1.3,
      text: recipe.diagnosis[demoHash(seed, ordinal, "diagnosis") % recipe.diagnosis.length],
    });
    tool(1.65, "Edit", recipe.file, {
      file_path: `/demo-repo/${recipe.file}`,
      old_string: recipe.before,
      new_string: recipe.after,
    }, `Updated ${recipe.file}`);
    events.push({
      type: "diff_applied",
      id: eventId("diff"),
      t: 2.05,
      file: recipe.file,
      before: recipe.before,
      after: recipe.after,
      line: recipe.line,
    });
    const passedCall = tool(2.45, "Bash", recipe.testCommand, { command: recipe.testCommand }, recipe.testOutput);
    events.push({
      type: "test_result",
      id: eventId("test"),
      t: 2.85,
      callId: passedCall,
      command: recipe.testCommand,
      output: recipe.testOutput,
      passed: true,
    });
  }

  events.push({ type: "completed", id: eventId("complete"), t: 4.35, summary: recipe.summary });
  events.push({ type: "ready", id: eventId("ready"), t: 5.0 });
  return events;
}

export function generateDemoStory(seed: string, ordinal: number): DemoStory {
  const safeOrdinal = Math.max(0, Math.floor(ordinal));
  const recipe = DEMO_RECIPES[demoRecipeIndex(seed, safeOrdinal, DEMO_RECIPES.length)];
  const events = buildEvents(seed, safeOrdinal, recipe);
  return {
    id: `${recipe.id}-${safeOrdinal}`,
    ordinal: safeOrdinal,
    recipeId: recipe.id,
    shortLabel: recipe.shortLabel,
    prompt: recipe.prompt,
    file: recipe.file,
    durationSec: DURATION_SEC,
    events,
    timeline: renderDemoTerminal(recipe.prompt, events, DURATION_SEC),
  };
}

export function getDemoStoryState(story: DemoStory, tSec: number): DemoStoryState {
  const time = Math.max(0, Math.min(story.durationSec, tSec));
  const visible = story.events.filter((event) => event.t <= time);
  const latest = visible.at(-1);
  const progress = time / story.durationSec;
  if (!latest || latest.type === "instruction_received") {
    return { phase: "queued", message: "Instruction received", detail: story.shortLabel, progress };
  }
  if (visible.some((event) => event.type === "completed")) {
    return { phase: "complete", message: "Ready for input", detail: story.shortLabel, progress: 1 };
  }
  if ((latest.type === "tool_result" && latest.failed) || (latest.type === "test_result" && !latest.passed)) {
    return { phase: "working", message: "Test failed, correcting", detail: story.file, progress };
  }
  if (latest.type === "test_result" && latest.passed) {
    return { phase: "working", message: "Tests passed", detail: "finishing task", progress };
  }
  if (latest.type === "tool_started") {
    const action = latest.tool === "Read" ? "Reading" : latest.tool === "Edit" ? "Editing" : latest.tool === "Bash" ? "Running tests" : "Searching";
    const detail = latest.tool === "Bash" ? "focused test suite" : latest.display;
    return { phase: "working", message: action, detail, progress };
  }
  return { phase: "working", message: "Working", detail: story.file, progress };
}
