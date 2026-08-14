export type Vec3 = readonly [number, number, number];

export type CameraFrame = {
  position: Vec3;
  target: Vec3;
  up: Vec3;
  verticalFovDegrees: number;
};

export type ProjectedPoint = {
  x: number;
  y: number;
  depth: number;
  visible: boolean;
};

export function add(a: Vec3, b: Vec3): Vec3 {
  return [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
}

export function subtract(a: Vec3, b: Vec3): Vec3 {
  return [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
}

export function scale(vector: Vec3, amount: number): Vec3 {
  return [vector[0] * amount, vector[1] * amount, vector[2] * amount];
}

export function dot(a: Vec3, b: Vec3): number {
  return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
}

export function cross(a: Vec3, b: Vec3): Vec3 {
  return [
    a[1] * b[2] - a[2] * b[1],
    a[2] * b[0] - a[0] * b[2],
    a[0] * b[1] - a[1] * b[0],
  ];
}

export function length(vector: Vec3): number {
  return Math.sqrt(dot(vector, vector));
}

export function normalize(vector: Vec3): Vec3 {
  const magnitude = length(vector);
  return magnitude > 1e-8 ? scale(vector, 1 / magnitude) : [0, 0, 0];
}

export function lerp(a: Vec3, b: Vec3, progress: number): Vec3 {
  return [
    a[0] + (b[0] - a[0]) * progress,
    a[1] + (b[1] - a[1]) * progress,
    a[2] + (b[2] - a[2]) * progress,
  ];
}

export function rotateAroundY(point: Vec3, pivot: Vec3, radians: number): Vec3 {
  const translated = subtract(point, pivot);
  const cosine = Math.cos(radians);
  const sine = Math.sin(radians);
  return add([
    translated[0] * cosine + translated[2] * sine,
    translated[1],
    -translated[0] * sine + translated[2] * cosine,
  ], pivot);
}

export function smoothstep(edge0: number, edge1: number, value: number): number {
  if (edge0 === edge1) return value < edge0 ? 0 : 1;
  const progress = Math.max(0, Math.min(1, (value - edge0) / (edge1 - edge0)));
  return progress * progress * (3 - 2 * progress);
}

export function projectPoint(
  point: Vec3,
  camera: CameraFrame,
  width: number,
  height: number,
): ProjectedPoint {
  const forward = normalize(subtract(camera.target, camera.position));
  const right = normalize(cross(forward, camera.up));
  const cameraUp = normalize(cross(right, forward));
  const relative = subtract(point, camera.position);
  const depth = dot(relative, forward);
  if (depth <= 0.05) return { x: 0, y: 0, depth, visible: false };

  const aspect = width / height;
  const tangent = Math.tan((camera.verticalFovDegrees * Math.PI) / 360);
  const normalizedX = dot(relative, right) / (depth * tangent * aspect);
  const normalizedY = dot(relative, cameraUp) / (depth * tangent);
  const x = (normalizedX * 0.5 + 0.5) * width;
  const y = (0.5 - normalizedY * 0.5) * height;
  return {
    x,
    y,
    depth,
    visible: normalizedX >= -1.2 && normalizedX <= 1.2 && normalizedY >= -1.2 && normalizedY <= 1.2,
  };
}
