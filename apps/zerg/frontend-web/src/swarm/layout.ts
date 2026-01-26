import type { SwarmBounds, SwarmGridPoint, SwarmMapLayout, SwarmPoint } from "./types";

export type SwarmIsoPoint = {
  x: number;
  y: number;
};

export function gridToIso(point: SwarmGridPoint, layout: SwarmMapLayout): SwarmIsoPoint {
  const { width, height } = layout.tile;
  const x = (point.col - point.row) * (width / 2) + layout.origin.x;
  const y = (point.col + point.row) * (height / 2) + layout.origin.y;
  return { x, y };
}

export function isoToGrid(point: SwarmPoint, layout: SwarmMapLayout): SwarmGridPoint {
  const { width, height } = layout.tile;
  const x = point.x - layout.origin.x;
  const y = point.y - layout.origin.y;
  const col = (x / (width / 2) + y / (height / 2)) / 2;
  const row = (y / (height / 2) - x / (width / 2)) / 2;
  return { col: Math.round(col), row: Math.round(row) };
}

export function clampToBounds(point: SwarmGridPoint, bounds: SwarmBounds): SwarmGridPoint {
  return {
    col: Math.min(bounds.maxCol, Math.max(bounds.minCol, point.col)),
    row: Math.min(bounds.maxRow, Math.max(bounds.minRow, point.row)),
  };
}

export function isPointInBounds(point: SwarmGridPoint, bounds: SwarmBounds): boolean {
  return (
    point.col >= bounds.minCol &&
    point.col <= bounds.maxCol &&
    point.row >= bounds.minRow &&
    point.row <= bounds.maxRow
  );
}
