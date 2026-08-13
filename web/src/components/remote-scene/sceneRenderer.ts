import { SCENE_SPEC, getSceneCamera } from "./sceneSpec";

type Grid = Uint8Array;

const { width: WIDTH, height: HEIGHT } = SCENE_SPEC;

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

function smoothstep(edge0: number, edge1: number, value: number): number {
  const t = clamp((value - edge0) / (edge1 - edge0), 0, 1);
  return t * t * (3 - 2 * t);
}

function project(x: number, y: number, timeSeconds: number): [number, number] {
  const camera = getSceneCamera(timeSeconds);
  return [
    (x - 50) * camera.scale + 50 + camera.lateral,
    camera.horizon + (y - camera.horizon) * camera.scale,
  ];
}

function put(grid: Grid, x: number, y: number, tone: number, overwrite = false): void {
  const column = Math.round(x);
  const row = Math.round(y);
  if (column < 0 || column >= WIDTH || row < 0 || row >= HEIGHT) return;
  const index = row * WIDTH + column;
  if (overwrite || tone > grid[index]) grid[index] = tone;
}

function worldPoint(grid: Grid, x: number, y: number, tone: number, timeSeconds: number): void {
  const [screenX, screenY] = project(x, y, timeSeconds);
  put(grid, screenX, screenY, tone);
}

function worldRect(
  grid: Grid,
  x: number,
  y: number,
  rectWidth: number,
  rectHeight: number,
  tone: number,
  timeSeconds: number,
  overwrite = false,
): void {
  const step = 0.55;
  for (let worldY = y; worldY < y + rectHeight; worldY += step) {
    for (let worldX = x; worldX < x + rectWidth; worldX += step) {
      const [screenX, screenY] = project(worldX, worldY, timeSeconds);
      const column = Math.round(screenX);
      const row = Math.round(screenY);
      if (column < 0 || column >= WIDTH || row < 0 || row >= HEIGHT) continue;
      const index = row * WIDTH + column;
      if (overwrite || tone > grid[index]) grid[index] = tone;
    }
  }
}

function worldLine(
  grid: Grid,
  x1: number,
  y1: number,
  x2: number,
  y2: number,
  tone: number,
  timeSeconds: number,
): void {
  const steps = Math.max(Math.abs(x2 - x1), Math.abs(y2 - y1)) * 2;
  for (let step = 0; step <= steps; step += 1) {
    const progress = steps === 0 ? 0 : step / steps;
    worldPoint(grid, x1 + (x2 - x1) * progress, y1 + (y2 - y1) * progress, tone, timeSeconds);
  }
}

function worldEllipse(
  grid: Grid,
  centerX: number,
  centerY: number,
  radiusX: number,
  radiusY: number,
  tone: number,
  timeSeconds: number,
): void {
  for (let y = -radiusY; y <= radiusY; y += 0.6) {
    for (let x = -radiusX; x <= radiusX; x += 0.6) {
      if ((x * x) / (radiusX * radiusX) + (y * y) / (radiusY * radiusY) <= 1) {
        worldPoint(grid, centerX + x, centerY + y, tone, timeSeconds);
      }
    }
  }
}

function drawBackground(grid: Grid, timeSeconds: number): void {
  for (let row = 0; row < HEIGHT; row += 1) {
    const tone = row < 25 ? (row % 4 === 0 ? 2 : 1) : row < 31 ? 2 : 3;
    for (let column = 0; column < WIDTH; column += 1) grid[row * WIDTH + column] = tone;
  }

  // A quiet wall seam and floor perspective keep the space legible without
  // turning the environment into a busy illustration.
  worldLine(grid, 0, 30.5, 100, 30.5, 4, timeSeconds);
  for (const endX of [7, 25, 45, 72, 94]) {
    worldLine(grid, 50, 30.5, endX, 56, 3, timeSeconds);
  }
  for (const floorY of [36, 42, 49]) worldLine(grid, 0, floorY, 100, floorY, 3, timeSeconds);
  worldRect(grid, 3, 8, 23, 19, 2, timeSeconds);
  worldRect(grid, 5, 10, 19, 15, 1, timeSeconds);
  worldLine(grid, 14.5, 10, 14.5, 25, 2, timeSeconds);
  worldLine(grid, 5, 17.5, 24, 17.5, 2, timeSeconds);
}

function drawDeskAndWorkstation(grid: Grid, frameIndex: number, timeSeconds: number): void {
  worldEllipse(grid, 65, 48, 29, 3.2, 2, timeSeconds);
  worldRect(grid, 39, 30, 53, 2.4, 5, timeSeconds);
  worldRect(grid, 40, 29.2, 51, 1.1, 6, timeSeconds);
  worldLine(grid, 44, 32, 42, 54, 4, timeSeconds);
  worldLine(grid, 85, 32, 88, 54, 4, timeSeconds);
  worldLine(grid, 55, 32, 54, 50, 3, timeSeconds);

  // Monitor bezel, screen, and base.
  worldRect(grid, 53, 12, 25, 18, 4, timeSeconds);
  worldRect(grid, 55, 14, 21, 14, 1, timeSeconds, true);
  worldRect(grid, 56, 15, 19, 12, 3, timeSeconds, true);
  worldRect(grid, 60, 29.5, 11, 1.2, 4, timeSeconds);
  worldRect(grid, 64.5, 30, 2, 2.2, 4, timeSeconds);

  const pulse = 0.5 + 0.5 * Math.sin(frameIndex * 0.8);
  for (let line = 0; line < 6; line += 1) {
    const length = 3 + ((line * 7 + frameIndex) % 12) * 0.75;
    const tone = line === 1 || line === 4 ? 8 : pulse > 0.45 ? 7 : 6;
    worldRect(grid, 57.5, 16.4 + line * 1.55, length, 0.6, tone, timeSeconds);
  }
  worldPoint(grid, 57.5, 26.1, 8, timeSeconds);
  worldRect(grid, 59, 25.7, 5 + (frameIndex % 4), 0.55, 8, timeSeconds);
  worldRect(grid, 75.5, 15.2, 0.5, 11.5, 7, timeSeconds);

  // Small desk objects establish scale and make the workstation feel occupied.
  worldRect(grid, 78, 27.3, 4, 2.8, 4, timeSeconds);
  worldRect(grid, 79, 25.5, 2, 2, 5, timeSeconds);
  worldEllipse(grid, 48, 28.4, 2.2, 1.5, 5, timeSeconds);
  worldLine(grid, 48, 27, 48, 24.8, 5, timeSeconds);
}

function drawDepartingPerson(grid: Grid, timeSeconds: number): void {
  const departure = smoothstep(0.65, 3.1, timeSeconds);
  const presence = 1 - smoothstep(2.55, 3.5, timeSeconds);
  if (presence <= 0) return;

  const personX = 38 - departure * 12;
  const tone = presence > 0.65 ? 5 : 4;
  worldEllipse(grid, personX, 23.5, 2.1, 2.1, tone, timeSeconds);
  worldLine(grid, personX, 25.6, personX - 1.3, 34, tone, timeSeconds);
  worldLine(grid, personX - 1.3, 28, personX - 5, 31.5, tone, timeSeconds);
  worldLine(grid, personX - 1.3, 28, personX + 2.6, 30.5, tone, timeSeconds);
  worldLine(grid, personX - 1.3, 34, personX - 4, 45, tone, timeSeconds);
  worldLine(grid, personX - 1.3, 34, personX + 2.8, 44, tone, timeSeconds);
  worldPoint(grid, personX - 4.8, 31.7, 6, timeSeconds);

  if (departure > 0.25 && departure < 0.9) {
    worldLine(grid, personX + 5, 36, personX + 9, 36, 3, timeSeconds);
    worldLine(grid, personX + 6, 38, personX + 11, 38, 3, timeSeconds);
  }
}

function drawPhone(grid: Grid, timeSeconds: number): void {
  const focus = smoothstep(2.1, 4.8, timeSeconds);
  const phoneTone = 5 + Math.round(focus * 3);
  worldEllipse(grid, 84, 50, 12 + focus * 2, 3, 2 + Math.round(focus), timeSeconds);
  worldRect(grid, 79, 37, 11, 18, 4, timeSeconds);
  worldRect(grid, 80.3, 38.4, 8.4, 15.2, 1, timeSeconds, true);
  worldRect(grid, 81, 40, 7, 12.3, phoneTone, timeSeconds, true);
  worldRect(grid, 82, 41, 4 + focus * 2.2, 0.7, 8, timeSeconds);
  worldRect(grid, 82, 43.2, 5.5, 0.5, focus > 0.4 ? 9 : 7, timeSeconds);
  worldRect(grid, 82, 45, 4.4, 0.5, 8, timeSeconds);
  worldLine(grid, 82, 49, 86, 49, 9, timeSeconds);
  worldPoint(grid, 84, 53, 7, timeSeconds);

  if (focus > 0.25) {
    worldLine(grid, 76, 43, 73, 41, 6, timeSeconds);
    worldLine(grid, 92, 43, 95, 41, 6, timeSeconds);
    worldLine(grid, 76, 48, 72, 49, 5, timeSeconds);
    worldLine(grid, 92, 48, 96, 49, 5, timeSeconds);
    worldPoint(grid, 84, 35, 7, timeSeconds);
  }
}

function drawAmbientLight(grid: Grid, timeSeconds: number): void {
  const glow = smoothstep(0, 2.5, timeSeconds);
  worldLine(grid, 90, 5, 90, 17, 4, timeSeconds);
  worldRect(grid, 84, 16, 12, 1.4, 5 + Math.round(glow), timeSeconds);
  worldLine(grid, 85, 17, 86, 26, 4, timeSeconds);
  worldLine(grid, 95, 17, 94, 25, 4, timeSeconds);
}

export function renderSceneFrame(frameIndex: number): Uint8Array {
  const totalFrames = SCENE_SPEC.durationSeconds * SCENE_SPEC.fps;
  const safeFrame = Math.max(0, Math.min(totalFrames - 1, frameIndex));
  const timeSeconds = safeFrame / SCENE_SPEC.fps;
  const grid = new Uint8Array(WIDTH * HEIGHT);

  drawBackground(grid, timeSeconds);
  drawAmbientLight(grid, timeSeconds);
  drawDeskAndWorkstation(grid, safeFrame, timeSeconds);
  drawDepartingPerson(grid, timeSeconds);
  drawPhone(grid, timeSeconds);
  return grid;
}
