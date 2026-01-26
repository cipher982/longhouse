import { describe, expect, it } from "vitest";
import { createForumReplayCursor, advanceForumReplay, generateForumReplay } from "../replay";

const scenario = generateForumReplay({
  seed: "cursor-seed",
  durationMs: 2000,
  tickMs: 1000,
  roomCount: 2,
  unitsPerRoom: 1,
  tasksPerRoom: 1,
  workersPerRoom: 1,
});

describe("forum replay cursor", () => {
  it("advances incrementally", () => {
    const cursor = createForumReplayCursor(scenario);
    const initialTasks = cursor.state.tasks.size;
    const applied = advanceForumReplay(cursor, 1000);
    expect(applied).toBeGreaterThan(0);
    expect(cursor.state.tasks.size).toBe(initialTasks);
    expect(cursor.now).toBeGreaterThanOrEqual(1000);
  });
});
