import {
  add,
  cross,
  dot,
  lerp,
  normalize,
  projectPoint,
  rotateAroundY,
  scale,
  smoothstep,
  subtract,
  type CameraFrame,
  type Vec3,
} from "./sceneMath";
import {
  SCENE_MATERIAL,
  SCENE_SPEC,
  getSceneCamera,
  getSceneProfile,
  type SceneProfileKey,
} from "./sceneSpec";

export type SourceRaster = {
  width: number;
  height: number;
  luminance: Float32Array;
  material: Uint8Array;
  depth: Float32Array;
  emissive: Uint8Array;
};

type SurfaceStyle = {
  material: number;
  brightness: number;
  emissive?: number;
};

const LIGHT_DIRECTION = normalize([0.35, 0.82, 0.45]);

export function createSourceRaster(width: number, height: number): SourceRaster {
  const cellCount = width * height;
  const depth = new Float32Array(cellCount);
  depth.fill(Number.POSITIVE_INFINITY);
  return {
    width,
    height,
    luminance: new Float32Array(cellCount),
    material: new Uint8Array(cellCount),
    depth,
    emissive: new Uint8Array(cellCount),
  };
}

function writeSample(
  raster: SourceRaster,
  x: number,
  y: number,
  depth: number,
  luminance: number,
  material: number,
  emissive = 0,
): void {
  const column = Math.round(x);
  const row = Math.round(y);
  if (column < 0 || column >= raster.width || row < 0 || row >= raster.height || depth <= 0) return;
  const index = row * raster.width + column;
  if (depth > raster.depth[index] + 0.015) return;
  raster.depth[index] = depth;
  raster.luminance[index] = Math.max(0, Math.min(1, luminance));
  raster.material[index] = material;
  raster.emissive[index] = emissive > 0 ? 1 : 0;
}

function triangleNormal(a: Vec3, b: Vec3, c: Vec3): Vec3 {
  return normalize(cross(subtract(b, a), subtract(c, a)));
}

export function drawTriangle3D(
  raster: SourceRaster,
  camera: CameraFrame,
  a: Vec3,
  b: Vec3,
  c: Vec3,
  style: SurfaceStyle,
): void {
  const projected = [
    projectPoint(a, camera, raster.width, raster.height),
    projectPoint(b, camera, raster.width, raster.height),
    projectPoint(c, camera, raster.width, raster.height),
  ] as const;
  if (projected.every((point) => !point.visible)) return;

  const minimumX = Math.max(0, Math.floor(Math.min(...projected.map((point) => point.x))));
  const maximumX = Math.min(raster.width - 1, Math.ceil(Math.max(...projected.map((point) => point.x))));
  const minimumY = Math.max(0, Math.floor(Math.min(...projected.map((point) => point.y))));
  const maximumY = Math.min(raster.height - 1, Math.ceil(Math.max(...projected.map((point) => point.y))));
  const [pa, pb, pc] = projected;
  const denominator = (pb.y - pc.y) * (pa.x - pc.x) + (pc.x - pb.x) * (pa.y - pc.y);
  if (Math.abs(denominator) < 1e-6) return;

  const normal = triangleNormal(a, b, c);
  const diffuse = Math.max(0, Math.abs(dot(normal, LIGHT_DIRECTION)));
  const light = style.emissive
    ? Math.min(1, style.brightness + style.emissive)
    : style.brightness * (0.36 + diffuse * 0.64);

  for (let y = minimumY; y <= maximumY; y += 1) {
    for (let x = minimumX; x <= maximumX; x += 1) {
      const sampleX = x + 0.5;
      const sampleY = y + 0.5;
      const alpha = ((pb.y - pc.y) * (sampleX - pc.x) + (pc.x - pb.x) * (sampleY - pc.y)) / denominator;
      const beta = ((pc.y - pa.y) * (sampleX - pc.x) + (pa.x - pc.x) * (sampleY - pc.y)) / denominator;
      const gamma = 1 - alpha - beta;
      if (alpha < -0.001 || beta < -0.001 || gamma < -0.001) continue;
      const depth = alpha * pa.depth + beta * pb.depth + gamma * pc.depth;
      writeSample(raster, x, y, depth, light, style.material, style.emissive);
    }
  }
}

function drawQuad3D(
  raster: SourceRaster,
  camera: CameraFrame,
  a: Vec3,
  b: Vec3,
  c: Vec3,
  d: Vec3,
  style: SurfaceStyle,
): void {
  drawTriangle3D(raster, camera, a, b, c, style);
  drawTriangle3D(raster, camera, a, c, d, style);
}

export function drawLine3D(
  raster: SourceRaster,
  camera: CameraFrame,
  start: Vec3,
  end: Vec3,
  style: SurfaceStyle,
  thickness = 1,
): void {
  const from = projectPoint(start, camera, raster.width, raster.height);
  const to = projectPoint(end, camera, raster.width, raster.height);
  if (!from.visible && !to.visible) return;
  const steps = Math.max(1, Math.ceil(Math.hypot(to.x - from.x, to.y - from.y) * 1.5));
  for (let step = 0; step <= steps; step += 1) {
    const progress = step / steps;
    const x = from.x + (to.x - from.x) * progress;
    const y = from.y + (to.y - from.y) * progress;
    const depth = from.depth + (to.depth - from.depth) * progress - 0.012;
    for (let offsetY = -thickness; offsetY <= thickness; offsetY += 1) {
      for (let offsetX = -thickness; offsetX <= thickness; offsetX += 1) {
        if (offsetX * offsetX + offsetY * offsetY > thickness * thickness + 0.25) continue;
        writeSample(
          raster,
          x + offsetX,
          y + offsetY,
          depth,
          Math.min(1, style.brightness + (style.emissive ?? 0)),
          style.material,
          style.emissive,
        );
      }
    }
  }
}

function drawDisc3D(
  raster: SourceRaster,
  camera: CameraFrame,
  center: Vec3,
  worldRadius: number,
  style: SurfaceStyle,
): void {
  const projectedCenter = projectPoint(center, camera, raster.width, raster.height);
  const projectedEdge = projectPoint(add(center, [worldRadius, 0, 0]), camera, raster.width, raster.height);
  if (!projectedCenter.visible) return;
  const radius = Math.max(1, Math.abs(projectedEdge.x - projectedCenter.x));
  for (let y = -radius; y <= radius; y += 1) {
    for (let x = -radius; x <= radius; x += 1) {
      if (x * x + y * y > radius * radius) continue;
      writeSample(
        raster,
        projectedCenter.x + x,
        projectedCenter.y + y,
        projectedCenter.depth - 0.018,
        Math.min(1, style.brightness + (style.emissive ?? 0)),
        style.material,
        style.emissive,
      );
    }
  }
}

function boxCorners(minimum: Vec3, maximum: Vec3): Vec3[] {
  return [
    [minimum[0], minimum[1], minimum[2]],
    [maximum[0], minimum[1], minimum[2]],
    [maximum[0], maximum[1], minimum[2]],
    [minimum[0], maximum[1], minimum[2]],
    [minimum[0], minimum[1], maximum[2]],
    [maximum[0], minimum[1], maximum[2]],
    [maximum[0], maximum[1], maximum[2]],
    [minimum[0], maximum[1], maximum[2]],
  ];
}

function drawBox(
  raster: SourceRaster,
  camera: CameraFrame,
  minimum: Vec3,
  maximum: Vec3,
  style: SurfaceStyle,
  outlineStyle?: SurfaceStyle,
): void {
  const corners = boxCorners(minimum, maximum);
  const faces = [
    [0, 1, 2, 3], [4, 7, 6, 5], [0, 4, 5, 1],
    [3, 2, 6, 7], [1, 5, 6, 2], [0, 3, 7, 4],
  ] as const;
  for (const [a, b, c, d] of faces) drawQuad3D(raster, camera, corners[a], corners[b], corners[c], corners[d], style);
  if (!outlineStyle) return;
  const edges = [
    [0, 1], [1, 2], [2, 3], [3, 0], [4, 5], [5, 6], [6, 7], [7, 4],
    [0, 4], [1, 5], [2, 6], [3, 7],
  ] as const;
  for (const [start, end] of edges) drawLine3D(raster, camera, corners[start], corners[end], outlineStyle);
}

function drawWireBox(
  raster: SourceRaster,
  camera: CameraFrame,
  minimum: Vec3,
  maximum: Vec3,
  style: SurfaceStyle,
): void {
  const corners = boxCorners(minimum, maximum);
  const edges = [
    [0, 1], [1, 2], [2, 3], [3, 0], [4, 5], [5, 6], [6, 7], [7, 4],
    [0, 4], [1, 5], [2, 6], [3, 7],
  ] as const;
  for (const [start, end] of edges) drawLine3D(raster, camera, corners[start], corners[end], style);
}

function drawRoom(raster: SourceRaster, camera: CameraFrame, timeSeconds: number): void {
  const dim = 1 - smoothstep(2.25, 3.25, timeSeconds) * 0.2;
  drawQuad3D(
    raster,
    camera,
    [-8, 0, 0], [8, 0, 0], [8, 6.2, 0], [-8, 6.2, 0],
    { material: SCENE_MATERIAL.ambient, brightness: 0.27 * dim },
  );
  drawQuad3D(
    raster,
    camera,
    [-8, 0, 0], [-8, 0, 9], [8, 0, 9], [8, 0, 0],
    { material: SCENE_MATERIAL.ambient, brightness: 0.2 * dim },
  );
  for (const depth of [1.5, 3.5, 5.5, 7.5]) {
    drawLine3D(raster, camera, [-8, 0.012, depth], [8, 0.012, depth], { material: SCENE_MATERIAL.ambient, brightness: 0.33 * dim });
  }
  for (const x of [-6, -3, 0, 3, 6]) {
    drawLine3D(raster, camera, [x, 0.012, 0], [x, 0.012, 9], { material: SCENE_MATERIAL.ambient, brightness: 0.29 * dim });
  }

  // Window and shallow skyline.
  drawQuad3D(raster, camera, [-2.8, 2.55, 0.02], [0.15, 2.55, 0.02], [0.15, 4.9, 0.02], [-2.8, 4.9, 0.02], { material: SCENE_MATERIAL.void, brightness: 0 });
  for (const edge of [
    [[-2.8, 2.55, 0.03], [0.15, 2.55, 0.03]], [[0.15, 2.55, 0.03], [0.15, 4.9, 0.03]],
    [[0.15, 4.9, 0.03], [-2.8, 4.9, 0.03]], [[-2.8, 4.9, 0.03], [-2.8, 2.55, 0.03]],
    [[-1.3, 2.55, 0.03], [-1.3, 4.9, 0.03]], [[-2.8, 3.7, 0.03], [0.15, 3.7, 0.03]],
  ] as const) drawLine3D(raster, camera, edge[0], edge[1], { material: SCENE_MATERIAL.structure, brightness: 0.72 * dim });

  // Door opening, frame, and rotating leaf.
  drawQuad3D(raster, camera, [-6.3, 0, 0.04], [-4.15, 0, 0.04], [-4.15, 4.45, 0.04], [-6.3, 4.45, 0.04], { material: SCENE_MATERIAL.void, brightness: 0 });
  drawLine3D(raster, camera, [-6.3, 0, 0.06], [-6.3, 4.45, 0.06], { material: SCENE_MATERIAL.active, brightness: 0.83 });
  drawLine3D(raster, camera, [-6.3, 4.45, 0.06], [-4.15, 4.45, 0.06], { material: SCENE_MATERIAL.active, brightness: 0.83 });
  drawLine3D(raster, camera, [-4.15, 4.45, 0.06], [-4.15, 0, 0.06], { material: SCENE_MATERIAL.structure, brightness: 0.78 });

  const close = smoothstep(2.18, 2.95, timeSeconds);
  const angle = (1 - close) * (Math.PI * 0.42);
  const pivot: Vec3 = [-6.25, 0, 0.12];
  const door = [
    pivot,
    rotateAroundY([-4.2, 0, 0.12], pivot, angle),
    rotateAroundY([-4.2, 4.35, 0.12], pivot, angle),
    [-6.25, 4.35, 0.12] as Vec3,
  ] as const;
  drawQuad3D(raster, camera, door[0], door[1], door[2], door[3], { material: SCENE_MATERIAL.ambient, brightness: 0.46 * dim });
  for (let index = 0; index < 4; index += 1) {
    drawLine3D(raster, camera, door[index], door[(index + 1) % 4], { material: SCENE_MATERIAL.structure, brightness: 0.78 * dim });
  }
  const handle = rotateAroundY([-4.45, 2.25, 0.15], pivot, angle);
  drawDisc3D(raster, camera, handle, 0.07, { material: SCENE_MATERIAL.highlight, brightness: 0.9 });
}

function drawFurniture(raster: SourceRaster, camera: CameraFrame, timeSeconds: number, frameIndex: number): void {
  const dim = 1 - smoothstep(2.25, 3.25, timeSeconds) * 0.14;
  const structure = { material: SCENE_MATERIAL.structure, brightness: 0.62 * dim };
  const outline = { material: SCENE_MATERIAL.structure, brightness: 0.82 * dim };
  // Keep the desk mostly skeletal. A fully shaded cuboid turns into an unreadable
  // field of glyphs at this resolution.
  drawQuad3D(
    raster,
    camera,
    [-0.45, 1.48, 1.3], [5.15, 1.48, 1.3], [5.15, 1.48, 3.55], [-0.45, 1.48, 3.55],
    { material: SCENE_MATERIAL.ambient, brightness: 0.22 * dim },
  );
  drawWireBox(raster, camera, [-0.45, 1.43, 1.3], [5.15, 1.58, 3.55], outline);
  for (const x of [-0.28, 4.98]) {
    drawLine3D(raster, camera, [x, 1.43, 1.45], [x, 0, 1.45], outline);
    drawLine3D(raster, camera, [x, 1.43, 3.4], [x, 0, 3.4], outline);
  }

  // Chair is deliberately offset from the actor's exit path.
  drawWireBox(raster, camera, [-0.9, 0.65, 2.05], [-0.05, 1.35, 2.85], outline);
  drawLine3D(raster, camera, [-0.48, 0.65, 2.45], [-0.48, 0.12, 2.45], outline);
  drawLine3D(raster, camera, [-0.48, 0.12, 2.45], [-1.1, 0, 1.95], outline);
  drawLine3D(raster, camera, [-0.48, 0.12, 2.45], [0.18, 0, 2.95], outline);

  // Monitor body and emissive face.
  drawBox(raster, camera, [1.1, 1.68, 1.18], [3.8, 4.05, 1.48], { material: SCENE_MATERIAL.ambient, brightness: 0.46 }, outline);
  drawQuad3D(raster, camera, [1.28, 1.9, 1.5], [3.62, 1.9, 1.5], [3.62, 3.84, 1.5], [1.28, 3.84, 1.5], { material: SCENE_MATERIAL.active, brightness: 0.58, emissive: 0.18 });
  drawBox(raster, camera, [2.2, 1.5, 1.25], [2.67, 1.75, 1.55], structure, outline);

  // A keyboard and mug make the horizontal plane read as a workstation at a glance.
  drawQuad3D(
    raster,
    camera,
    [1.15, 1.61, 2.18], [3.2, 1.61, 2.18], [3.05, 1.61, 2.72], [1.3, 1.61, 2.72],
    { material: SCENE_MATERIAL.structure, brightness: 0.42 * dim },
  );
  drawLine3D(raster, camera, [1.3, 1.63, 2.35], [3.05, 1.63, 2.35], outline);
  drawBox(
    raster,
    camera,
    [3.5, 1.58, 2.08], [3.88, 2.02, 2.45],
    { material: SCENE_MATERIAL.ambient, brightness: 0.35 * dim },
    outline,
  );

  const progress = Math.max(0, Math.min(1, (frameIndex - 36) / (124 - 36)));
  drawLine3D(raster, camera, [1.43, 2.12, 1.53], [1.43 + progress * 1.98, 2.12, 1.53], { material: SCENE_MATERIAL.active, brightness: 0.95, emissive: 0.2 }, 1);

  // Phone body is a vertical object on the desk, angled slightly toward camera.
  drawBox(raster, camera, [4.0, 1.68, 2.35], [4.92, 3.02, 2.58], { material: SCENE_MATERIAL.ambient, brightness: 0.58 }, outline);
  drawQuad3D(raster, camera, [4.09, 1.8, 2.6], [4.83, 1.8, 2.6], [4.83, 2.91, 2.6], [4.09, 2.91, 2.6], { material: SCENE_MATERIAL.structure, brightness: 0.48 });

  const phoneFocus = smoothstep(3.6, 5.2, timeSeconds);
  if (phoneFocus > 0) {
    drawLine3D(raster, camera, [4.18, 2.12, 2.62], [4.72, 2.12, 2.62], { material: SCENE_MATERIAL.active, brightness: 0.65 + phoneFocus * 0.3, emissive: 0.15 }, 1);
  }

  // Pendant and pool of light.
  drawLine3D(raster, camera, [4.65, 6.1, 2.15], [4.65, 4.85, 2.15], { material: SCENE_MATERIAL.structure, brightness: 0.58 * dim });
  drawLine3D(raster, camera, [4.65, 4.85, 2.15], [4.2, 4.45, 2.15], outline);
  drawLine3D(raster, camera, [4.65, 4.85, 2.15], [5.1, 4.45, 2.15], outline);
  drawLine3D(raster, camera, [4.2, 4.45, 2.15], [5.1, 4.45, 2.15], outline);
}

function drawActor(raster: SourceRaster, camera: CameraFrame, timeSeconds: number): void {
  if (timeSeconds > 2.82) return;
  const departure = smoothstep(0.48, 2.42, timeSeconds);
  // The walk happens across open floor in front of the desk so the silhouette
  // remains readable rather than dissolving into furniture glyphs.
  const position = lerp([0.15, 0, 4.75], [-5.25, 0, 0.72], departure);
  const stride = Math.sin(departure * Math.PI * 7);
  const bob = Math.abs(Math.sin(departure * Math.PI * 7)) * 0.08;
  const style = { material: SCENE_MATERIAL.highlight, brightness: 0.84, emissive: 0.02 };
  const hip = add(position, [0, 1.22 + bob, 0]);
  const shoulder = add(position, [0, 2.25 + bob, 0]);
  const head = add(position, [0, 2.87 + bob, 0]);
  drawDisc3D(raster, camera, head, 0.24, style);
  drawLine3D(raster, camera, add(shoulder, [-0.27, 0, 0]), add(shoulder, [0.27, 0, 0]), style, 0);
  drawLine3D(raster, camera, shoulder, hip, style, 0);
  drawLine3D(raster, camera, add(shoulder, [0, -0.08, 0]), add(position, [-0.43 - stride * 0.16, 1.55 + bob, 0.08]), style, 0);
  drawLine3D(raster, camera, add(shoulder, [0, -0.08, 0]), add(position, [0.43 + stride * 0.16, 1.58 + bob, -0.08]), style, 0);
  drawLine3D(raster, camera, hip, add(position, [-0.38 - stride * 0.25, 0.05, 0.12]), style, 0);
  drawLine3D(raster, camera, hip, add(position, [0.38 + stride * 0.25, 0.05, -0.12]), style, 0);
}

function drawCompletion(raster: SourceRaster, camera: CameraFrame, timeSeconds: number): void {
  const completion = smoothstep(5.12, 5.45, timeSeconds);
  if (completion <= 0) return;
  const center: Vec3 = [4.46, 2.35, 2.64];
  const style = { material: SCENE_MATERIAL.active, brightness: 0.86, emissive: 0.13 };
  for (let ray = 0; ray < 12; ray += 1) {
    const angle = (ray / 12) * Math.PI * 2;
    const inner = add(center, [Math.cos(angle) * (0.7 + completion * 0.12), Math.sin(angle) * 0.65, 0]);
    const outer = add(center, [Math.cos(angle) * (0.95 + completion * 0.18), Math.sin(angle) * 0.88, 0]);
    drawLine3D(raster, camera, inner, outer, style);
  }
}

export function renderSourceScene(profileKey: SceneProfileKey, frameIndex: number): SourceRaster {
  const profile = getSceneProfile(profileKey);
  const width = profile.width * SCENE_SPEC.samplesPerCell.x;
  const height = profile.height * SCENE_SPEC.samplesPerCell.y;
  const raster = createSourceRaster(width, height);
  const timeSeconds = frameIndex / SCENE_SPEC.fps;
  const camera = getSceneCamera(profileKey, timeSeconds);

  drawRoom(raster, camera, timeSeconds);
  drawFurniture(raster, camera, timeSeconds, frameIndex);
  drawActor(raster, camera, timeSeconds);
  drawCompletion(raster, camera, timeSeconds);
  return raster;
}
