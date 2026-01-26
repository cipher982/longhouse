import { describe, expect, it } from "vitest";
import { generateForumReplay } from "../replay";
import { applyForumEvents, createForumState } from "../state";
import {
  mapSupervisorComplete,
  mapSupervisorStarted,
  mapWorkerComplete,
  mapWorkerSpawned,
  mapWorkerToolFailed,
} from "../live-mapper";
import type { ForumReplayEventInput } from "../types";

const scenario = generateForumReplay({
  seed: "live-map",
  durationMs: 2000,
  tickMs: 1000,
  roomCount: 1,
  unitsPerRoom: 0,
  tasksPerRoom: 0,
  workersPerRoom: 0,
  workspaceCount: 1,
  repoGroupsPerWorkspace: 1,
});

const createSequencer = () => {
  let seq = 0;
  return (events: ForumReplayEventInput[]) =>
    events.map((event) => ({
      ...event,
      id: `evt-${seq}`,
      seq: seq++,
    }));
};

const createBaseState = () =>
  createForumState({
    layout: scenario.layout,
    workspaces: scenario.workspaces,
    repoGroups: scenario.repoGroups,
    rooms: scenario.rooms,
  });

describe("forum live mapper", () => {
  it("maps supervisor start into task + node", () => {
    const state = createBaseState();
    const toReplayEvents = createSequencer();
    const inputs = mapSupervisorStarted(state, {
      runId: 42,
      task: "Ship logs",
      timestamp: 1000,
    });

    applyForumEvents(state, toReplayEvents(inputs));

    const task = state.tasks.get("run-42");
    expect(task?.title).toBe("Ship logs");
    expect(state.entities.has("task-node-run-42")).toBe(true);
  });

  it("maps worker spawn + completion into worker and task updates", () => {
    const state = createBaseState();
    const toReplayEvents = createSequencer();

    const spawnInputs = mapWorkerSpawned(state, {
      jobId: 7,
      task: "Lint repo",
      timestamp: 1100,
    });
    applyForumEvents(state, toReplayEvents(spawnInputs));

    expect(state.workers.has("worker-7")).toBe(true);
    expect(state.tasks.has("job-7")).toBe(true);

    const completeInputs = mapWorkerComplete(state, {
      jobId: 7,
      status: "failed",
      timestamp: 1200,
    });
    applyForumEvents(state, toReplayEvents(completeInputs));

    const task = state.tasks.get("job-7");
    expect(task?.status).toBe("failed");
    expect(state.alerts.size).toBe(1);
  });

  it("maps supervisor completion into task resolve", () => {
    const state = createBaseState();
    const toReplayEvents = createSequencer();
    const startInputs = mapSupervisorStarted(state, {
      runId: 3,
      task: "Plan sprint",
      timestamp: 900,
    });
    applyForumEvents(state, toReplayEvents(startInputs));

    const completeInputs = mapSupervisorComplete(state, {
      runId: 3,
      result: "done",
      status: "success",
      timestamp: 1500,
    });
    applyForumEvents(state, toReplayEvents(completeInputs));

    expect(state.tasks.get("run-3")?.status).toBe("success");
  });

  it("maps tool failure into alert", () => {
    const state = createBaseState();
    const toReplayEvents = createSequencer();
    const inputs = mapWorkerToolFailed(state, {
      workerId: "worker-1",
      toolName: "fetch",
      toolCallId: "call-1",
      durationMs: 2000,
      error: "boom",
      timestamp: 1300,
    });
    applyForumEvents(state, toReplayEvents(inputs));

    expect(state.alerts.size).toBe(1);
  });
});
