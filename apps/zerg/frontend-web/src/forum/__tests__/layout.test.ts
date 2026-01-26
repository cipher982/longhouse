import { describe, expect, it } from "vitest";
import { clampToBounds, gridToIso, isoToGrid } from "../layout";
import type { ForumMapLayout } from "../types";

const layout: ForumMapLayout = {
  id: "layout-test",
  name: "Test",
  grid: { cols: 20, rows: 20 },
  tile: { width: 64, height: 32 },
  origin: { x: 0, y: 0 },
};

describe("forum layout", () => {
  it("round-trips grid to iso", () => {
    const point = { col: 7, row: 4 };
    const iso = gridToIso(point, layout);
    const back = isoToGrid(iso, layout);
    expect(back).toEqual(point);
  });

  it("clamps grid points", () => {
    const bounds = { minCol: 2, minRow: 3, maxCol: 6, maxRow: 8 };
    const clamped = clampToBounds({ col: 10, row: 1 }, bounds);
    expect(clamped).toEqual({ col: 6, row: 3 });
  });
});
