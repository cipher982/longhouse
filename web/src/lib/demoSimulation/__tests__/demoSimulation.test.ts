import { describe, expect, it } from "vitest";
import { buildTimelineModel } from "../../sessionWorkspace";
import {
  DEMO_RECIPES,
  demoRecipeIndex,
  demoStoryToProjection,
  generateDemoStory,
  resolveDemoSeed,
} from "..";

describe("procedural demo stories", () => {
  it("is byte-stable and independent of global Math.random", () => {
    const before = Math.random;
    Math.random = () => { throw new Error("demo simulation must not use Math.random"); };
    try {
      const left = generateDemoStory("stable-seed", 17);
      const right = generateDemoStory("stable-seed", 17);
      expect(JSON.stringify(left)).toBe(JSON.stringify(right));
    } finally {
      Math.random = before;
    }
  });

  it("covers every recipe before repeating and never repeats adjacent stories", () => {
    for (const seed of ["alpha", "beta", "gamma", "42"]) {
      const firstPass = Array.from(
        { length: DEMO_RECIPES.length },
        (_, ordinal) => demoRecipeIndex(seed, ordinal, DEMO_RECIPES.length),
      );
      expect(new Set(firstPass).size).toBe(DEMO_RECIPES.length);
      for (let ordinal = 1; ordinal < DEMO_RECIPES.length * 4; ordinal += 1) {
        expect(demoRecipeIndex(seed, ordinal, DEMO_RECIPES.length))
          .not.toBe(demoRecipeIndex(seed, ordinal - 1, DEMO_RECIPES.length));
      }
    }
  });

  it("satisfies event and terminal invariants across 1,000 stories", () => {
    for (let ordinal = 0; ordinal < 1_000; ordinal += 1) {
      const story = generateDemoStory(`property-${ordinal % 37}`, ordinal);
      const ids = new Set<string>();
      const started = new Set<string>();
      let previousTime = -1;
      let sawPassingTest = false;

      for (const event of story.events) {
        expect(event.t).toBeGreaterThanOrEqual(previousTime);
        expect(ids.has(event.id)).toBe(false);
        ids.add(event.id);
        previousTime = event.t;
        if (event.type === "tool_started") started.add(event.callId);
        if (event.type === "tool_result") expect(started.has(event.callId)).toBe(true);
        if (event.type === "test_result" && event.passed) sawPassingTest = true;
        if (event.type === "completed") expect(sawPassingTest).toBe(true);
      }

      const { timeline } = story;
      expect(timeline.meta).toMatchObject({
        cols: 64,
        rows: 14,
        prompt: story.prompt,
        promptIdleSec: 0,
        promptTypedSec: 0.2,
      });
      expect(timeline.states.length).toBeGreaterThan(0);
      expect(timeline.states[0].t).toBe(0);
      for (let index = 0; index < timeline.states.length; index += 1) {
        const state = timeline.states[index];
        expect(state.rows).toHaveLength(14);
        if (index > 0) expect(state.t).toBeGreaterThanOrEqual(timeline.states[index - 1].t);
        for (const rowIndex of state.rows) {
          expect(rowIndex).toBeGreaterThanOrEqual(0);
          expect(rowIndex).toBeLessThan(timeline.rowPool.length);
          const width = timeline.rowPool[rowIndex].reduce((sum, run) => sum + run.n, 0);
          expect(width).toBeLessThanOrEqual(64);
        }
      }
    }
  });

  it("includes a visible failed test followed by a passing retry", () => {
    const retryOrdinal = Array.from({ length: DEMO_RECIPES.length }, (_, ordinal) => ordinal)
      .find((ordinal) => generateDemoStory("retry-proof", ordinal).recipeId === "retry-backoff");
    expect(retryOrdinal).toBeDefined();
    const results = generateDemoStory("retry-proof", retryOrdinal!).events
      .filter((event) => event.type === "test_result");
    expect(results.map((event) => event.type === "test_result" && event.passed)).toEqual([false, true]);
  });

  it("adapts tool calls into the existing Longhouse timeline projection", () => {
    const story = generateDemoStory("projection", 0);
    const model = buildTimelineModel(demoStoryToProjection(story).items);
    expect(model.items.some((item) => item.kind === "message" && item.event.role === "user")).toBe(true);
    expect(model.toolItems.length).toBeGreaterThanOrEqual(3);
    expect(model.toolItems.every((interaction) => interaction.pairing === "id")).toBe(true);
    expect(model.items.some((item) => item.kind === "message" && item.event.content_text === story.events.find((event) => event.type === "completed")?.summary)).toBe(true);
  });
});

describe("demo seed resolution", () => {
  it("prefers demoSeed input over persisted state", () => {
    const storage = {
      getItem: () => "stored",
      setItem: () => undefined,
    };
    expect(resolveDemoSeed(" qa-seed ", storage)).toBe("qa-seed");
  });

  it("reuses persisted state and tolerates privacy-mode storage failures", () => {
    expect(resolveDemoSeed(null, {
      getItem: () => "persisted",
      setItem: () => undefined,
    })).toBe("persisted");
    expect(() => resolveDemoSeed(null, {
      getItem: () => { throw new Error("blocked"); },
      setItem: () => { throw new Error("blocked"); },
    })).not.toThrow();
  });
});
