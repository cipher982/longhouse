import { describe, expect, it } from "vitest";
import { generateSwarmReplay, hydrateSwarmReplay } from "../replay";

const baseConfig = {
  seed: "demo-seed",
  durationMs: 4000,
  tickMs: 1000,
  roomCount: 3,
  unitsPerRoom: 2,
  tasksPerRoom: 2,
  workersPerRoom: 1,
  workspaceCount: 1,
  repoGroupsPerWorkspace: 2,
};

describe("swarm replay", () => {
  it("generates deterministic replays for the same seed", () => {
    const scenarioA = generateSwarmReplay(baseConfig);
    const scenarioB = generateSwarmReplay(baseConfig);
    expect(JSON.stringify(scenarioA)).toEqual(JSON.stringify(scenarioB));
  });

  it("hydrates expected entity counts", () => {
    const scenario = generateSwarmReplay(baseConfig);
    const state = hydrateSwarmReplay(scenario);
    const expectedEntitiesPerRoom = baseConfig.unitsPerRoom + 2 + baseConfig.workersPerRoom;
    expect(state.rooms.size).toBe(baseConfig.roomCount);
    expect(state.entities.size).toBe(baseConfig.roomCount * expectedEntitiesPerRoom);
    expect(state.tasks.size).toBe(baseConfig.roomCount * baseConfig.tasksPerRoom);
    expect(state.workers.size).toBe(baseConfig.roomCount * baseConfig.workersPerRoom);
  });
});
