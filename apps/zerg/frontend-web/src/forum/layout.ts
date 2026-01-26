import type { ForumBounds, ForumGridPoint, ForumMapLayout, ForumPoint } from "./types";

export type ForumIsoPoint = {
  x: number;
  y: number;
};

export function gridToIso(point: ForumGridPoint, layout: ForumMapLayout): ForumIsoPoint {
  const { width, height } = layout.tile;
  const x = (point.col - point.row) * (width / 2) + layout.origin.x;
  const y = (point.col + point.row) * (height / 2) + layout.origin.y;
  return { x, y };
}

export function isoToGrid(point: ForumPoint, layout: ForumMapLayout): ForumGridPoint {
  const { width, height } = layout.tile;
  const x = point.x - layout.origin.x;
  const y = point.y - layout.origin.y;
  const col = (x / (width / 2) + y / (height / 2)) / 2;
  const row = (y / (height / 2) - x / (width / 2)) / 2;
  return { col: Math.round(col), row: Math.round(row) };
}

export function clampToBounds(point: ForumGridPoint, bounds: ForumBounds): ForumGridPoint {
  return {
    col: Math.min(bounds.maxCol, Math.max(bounds.minCol, point.col)),
    row: Math.min(bounds.maxRow, Math.max(bounds.minRow, point.row)),
  };
}

export function isPointInBounds(point: ForumGridPoint, bounds: ForumBounds): boolean {
  return (
    point.col >= bounds.minCol &&
    point.col <= bounds.maxCol &&
    point.row >= bounds.minRow &&
    point.row <= bounds.maxRow
  );
}
