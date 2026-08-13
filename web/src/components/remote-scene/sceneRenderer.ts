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
  const roomSettled = smoothstep(2.8, 3.8, timeSeconds) > 0.5;
  for (let row = 0; row < HEIGHT; row += 1) {
    for (let column = 0; column < WIDTH; column += 1) {
      const wallMark = row < 31 && row % 3 === 0 && column % 4 === 0;
      const floorMark = row >= 31 && (row + column) % (roomSettled ? 5 : 4) === 0;
      grid[row * WIDTH + column] = wallMark ? 1 : floorMark ? 2 : 0;
    }
  }

  // A quiet wall seam and floor perspective keep the space legible without
  // turning the environment into a busy illustration.
  worldLine(grid, 0, 30.5, 100, 30.5, 4, timeSeconds);
  for (const endX of [7, 25, 45, 72, 94]) {
    worldLine(grid, 50, 30.5, endX, 56, 3, timeSeconds);
  }
  for (const floorY of [36, 42, 49]) worldLine(grid, 0, floorY, 100, floorY, 3, timeSeconds);
  // An open doorway gives the walk a visible destination.
  worldRect(grid, 3, 11, 14, 36, 0, timeSeconds, true);
  worldLine(grid, 3, 47, 3, 11, 8, timeSeconds);
  worldLine(grid, 3, 11, 17, 11, 8, timeSeconds);
  worldLine(grid, 17, 11, 17, 47, 7, timeSeconds);
  worldLine(grid, 4, 46, 16, 33, 3, timeSeconds);
  worldLine(grid, 16, 46, 16, 33, 3, timeSeconds);
  worldPoint(grid, 15.5, 29, 8, timeSeconds);

  // A mullioned window and skyline make the wall read as a room.
  worldRect(grid, 18, 9, 20, 15, 1, timeSeconds, true);
  worldLine(grid, 18, 9, 38, 9, 6, timeSeconds);
  worldLine(grid, 18, 24, 38, 24, 6, timeSeconds);
  worldLine(grid, 18, 9, 18, 24, 6, timeSeconds);
  worldLine(grid, 38, 9, 38, 24, 6, timeSeconds);
  worldLine(grid, 28, 9, 28, 24, 3, timeSeconds);
  worldLine(grid, 18, 16.5, 38, 16.5, 3, timeSeconds);
  for (const [x, height] of [[20, 4], [23, 7], [27, 3], [30, 6], [34, 8]] as const) {
    worldRect(grid, x, 24 - height, 2, height, 2, timeSeconds);
  }
}

function drawChair(grid: Grid, timeSeconds: number): void {
  worldRect(grid, 30, 27, 7, 9, 4, timeSeconds);
  worldRect(grid, 29, 35, 9, 2, 5, timeSeconds);
  worldLine(grid, 33.5, 37, 33.5, 45, 4, timeSeconds);
  worldLine(grid, 33.5, 44, 28.5, 47, 4, timeSeconds);
  worldLine(grid, 33.5, 44, 38.5, 47, 4, timeSeconds);
  worldPoint(grid, 28, 47.5, 5, timeSeconds);
  worldPoint(grid, 39, 47.5, 5, timeSeconds);
}

function drawDeskAndWorkstation(grid: Grid, frameIndex: number, timeSeconds: number): void {
  worldEllipse(grid, 65, 48, 29, 3.2, 2, timeSeconds);
  worldRect(grid, 38, 30, 54, 2.4, 6, timeSeconds);
  worldRect(grid, 39, 29.2, 52, 1.1, 7, timeSeconds);
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
  const workPhase = frameIndex < 18 ? 0 : frameIndex < 38 ? 1 : frameIndex < 56 ? 2 : 3;
  const workProgress = clamp((frameIndex - 18) / (56 - 18), 0, 1);
  for (let line = 0; line < 6; line += 1) {
    const length = 4 + ((line * 5 + frameIndex + workPhase * 3) % 11) * 0.72;
    const tone = line === 1 || line === 4 ? 8 : pulse > 0.45 ? 7 : 6;
    worldRect(grid, 57.5, 16.4 + line * 1.55, length, 0.6, tone, timeSeconds);
  }
  worldPoint(grid, 57.5, 26.1, 8, timeSeconds);
  worldRect(grid, 59, 25.7, Math.max(1, 14 * workProgress), 0.55, workProgress >= 1 ? 9 : 8, timeSeconds);
  worldRect(grid, 75.5, 15.2, 0.5, 11.5, 7, timeSeconds);

  // Small desk objects establish scale and make the workstation feel occupied.
  worldRect(grid, 78, 27.3, 4, 2.8, 4, timeSeconds);
  worldRect(grid, 79, 25.5, 2, 2, 5, timeSeconds);
  worldEllipse(grid, 48, 28.4, 2.2, 1.5, 5, timeSeconds);
  worldLine(grid, 48, 27, 48, 24.8, 5, timeSeconds);
}

function drawDepartingPerson(grid: Grid, timeSeconds: number): void {
  // Hold long enough to establish the figure, then make translation dominate the
  // walk cycle so the exit reads in a low-frame-rate contact sheet.
  const departure = smoothstep(0.8, 2.45, timeSeconds);
  const personX = 36 - departure * 28;

  const stride = Math.sin(timeSeconds * 8.5);
  const tone = 9;
  worldEllipse(grid, personX, 23.5, 2.1, 2.1, tone, timeSeconds);
  worldLine(grid, personX, 25.6, personX - 0.8, 34, tone, timeSeconds);
  worldLine(grid, personX - 0.4, 28, personX - 3.6 - stride * 0.7, 31.5, tone, timeSeconds);
  worldLine(grid, personX - 0.4, 28, personX + 3 + stride * 0.6, 30.8, tone, timeSeconds);
  worldLine(grid, personX - 0.8, 34, personX - 4.1 - stride, 45, tone, timeSeconds);
  worldLine(grid, personX - 0.8, 34, personX + 3.5 + stride, 44, tone, timeSeconds);
  worldPoint(grid, personX - 3.8 - stride * 0.7, 31.7, 8, timeSeconds);
}

function drawDoorForeground(grid: Grid, timeSeconds: number): void {
  // Repaint the jamb after the figure so the body visibly passes through it,
  // then close the door behind them to make the exit unambiguous.
  worldLine(grid, 3, 47, 3, 11, 8, timeSeconds);
  worldLine(grid, 3, 11, 17, 11, 8, timeSeconds);
  worldLine(grid, 17, 11, 17, 47, 7, timeSeconds);
  const close = smoothstep(2.15, 2.85, timeSeconds);
  if (close > 0) {
    worldRect(grid, 3, 12, 13.5 * close, 34, 2, timeSeconds, true);
    worldLine(grid, 3 + 13.5 * close, 12, 3 + 13.5 * close, 46, 7, timeSeconds);
    if (close > 0.85) worldPoint(grid, 14.5, 29, 8, timeSeconds);
  }
}

function drawPhone(grid: Grid, frameIndex: number, timeSeconds: number): void {
  const focus = smoothstep(2.1, 4.8, timeSeconds);
  const phoneTone = 5 + Math.round(focus * 3);
  const completionPulse = frameIndex >= 56 && frameIndex % 8 < 4 ? 9 : 8;
  const workProgress = clamp((frameIndex - 18) / (56 - 18), 0, 1);
  worldEllipse(grid, 79.5, 51, 13 + focus * 2, 3, 2 + Math.round(focus), timeSeconds);
  worldRect(grid, 71, 33, 16, 22, 5, timeSeconds);
  worldRect(grid, 72.2, 34.2, 13.6, 19.6, 1, timeSeconds, true);
  worldRect(grid, 73, 36, 12, 16.4, phoneTone, timeSeconds, true);
  worldRect(grid, 74, 37.2, 6 + focus * 3, 0.7, 8, timeSeconds);
  worldRect(grid, 74, 40.2, 9.5, 0.6, focus > 0.4 ? 9 : 7, timeSeconds);
  worldRect(grid, 74, 43, 7.5, 0.6, 8, timeSeconds);
  worldRect(grid, 74, 46, 9, 0.6, frameIndex >= 38 ? completionPulse : 7, timeSeconds);
  worldLine(grid, 75, 49.5, 75 + Math.max(1, 8 * workProgress), 49.5, workProgress >= 1 ? 9 : 8, timeSeconds);
  worldPoint(grid, 79, 53.3, 7, timeSeconds);

  if (focus > 0.25) {
    worldLine(grid, 69, 40, 66, 38, 6, timeSeconds);
    worldLine(grid, 89, 40, 92, 38, 6, timeSeconds);
    worldLine(grid, 69, 47, 65, 48, 5, timeSeconds);
    worldLine(grid, 89, 47, 93, 48, 5, timeSeconds);
    worldPoint(grid, 79, 31, 7, timeSeconds);
  }
}

function drawAmbientLight(grid: Grid, timeSeconds: number): void {
  const glow = smoothstep(0, 2.5, timeSeconds);
  // A pendant light ends at the shade, avoiding the old ambiguous tower shape.
  worldLine(grid, 89, 0, 89, 9, 4, timeSeconds);
  worldLine(grid, 89, 9, 83, 15, 5 + Math.round(glow), timeSeconds);
  worldLine(grid, 89, 9, 95, 15, 5 + Math.round(glow), timeSeconds);
  worldLine(grid, 83, 15, 95, 15, 7, timeSeconds);
  worldEllipse(grid, 89, 16, 1.4, 1, 7 + Math.round(glow), timeSeconds);
  worldLine(grid, 85, 29, 89, 17, 3 + Math.round(glow), timeSeconds);
  worldLine(grid, 93, 29, 89, 17, 3 + Math.round(glow), timeSeconds);
}

function drawCompletionBeat(grid: Grid, frameIndex: number, timeSeconds: number): void {
  const completion = smoothstep(4.55, 5.15, timeSeconds);
  if (completion <= 0) return;
  const tone = frameIndex % 8 < 4 ? 9 : 8;
  const radius = 8 + completion * 3;
  for (let ray = 0; ray < 12; ray += 1) {
    const angle = (ray / 12) * Math.PI * 2;
    worldLine(
      grid,
      79 + Math.cos(angle) * radius,
      44 + Math.sin(angle) * radius * 0.55,
      79 + Math.cos(angle) * (radius + 2),
      44 + Math.sin(angle) * (radius + 2) * 0.55,
      tone,
      timeSeconds,
    );
  }
}

export function renderSceneFrame(frameIndex: number): Uint8Array {
  const totalFrames = SCENE_SPEC.durationSeconds * SCENE_SPEC.fps;
  const safeFrame = Math.max(0, Math.min(totalFrames - 1, frameIndex));
  const timeSeconds = safeFrame / SCENE_SPEC.fps;
  const grid = new Uint8Array(WIDTH * HEIGHT);

  drawBackground(grid, timeSeconds);
  drawAmbientLight(grid, timeSeconds);
  drawChair(grid, timeSeconds);
  drawDeskAndWorkstation(grid, safeFrame, timeSeconds);
  drawDepartingPerson(grid, timeSeconds);
  drawDoorForeground(grid, timeSeconds);
  drawPhone(grid, safeFrame, timeSeconds);
  drawCompletionBeat(grid, safeFrame, timeSeconds);
  return grid;
}
