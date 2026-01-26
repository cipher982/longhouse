import { describe, expect, it } from "vitest";
import { createSwarmReplayCursor, advanceSwarmReplay, generateSwarmReplay } from "../replay";

const scenario = generateSwarmReplay({
  seed: "cursor-seed",
  durationMs: 2000,
  tickMs: 1000,
  roomCount: 2,
  unitsPerRoom: 1,
  tasksPerRoom: 1,
  workersPerRoom: 1,
});

describe("swarm replay cursor", () => {
  it("advances incrementally", () => {
    const cursor = createSwarmReplayCursor(scenario);
    const initialTasks = cursor.state.tasks.size;
    const applied = advanceSwarmReplay(cursor, 1000);
    expect(applied).toBeGreaterThan(0);
    expect(cursor.state.tasks.size).toBe(initialTasks);
    expect(cursor.now).toBeGreaterThanOrEqual(1000);
  });
});
