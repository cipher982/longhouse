import { describe, expect, it } from "vitest";
import { generateSwarmReplay } from "../replay";
import { applySwarmEvents, createSwarmState } from "../state";

const scenario = generateSwarmReplay({
  seed: "state-seed",
  durationMs: 2000,
  tickMs: 1000,
  roomCount: 2,
  unitsPerRoom: 1,
  tasksPerRoom: 1,
  workersPerRoom: 1,
});

describe("swarm state", () => {
  it("applies events idempotently", () => {
    const state = createSwarmState({
      layout: scenario.layout,
      workspaces: scenario.workspaces,
      repoGroups: scenario.repoGroups,
      rooms: scenario.rooms,
    });

    applySwarmEvents(state, scenario.events);
    const entitiesAfterFirst = state.entities.size;
    const tasksAfterFirst = state.tasks.size;
    const alertsAfterFirst = state.alerts.size;

    applySwarmEvents(state, scenario.events);
    expect(state.entities.size).toBe(entitiesAfterFirst);
    expect(state.tasks.size).toBe(tasksAfterFirst);
    expect(state.alerts.size).toBe(alertsAfterFirst);
  });
});
