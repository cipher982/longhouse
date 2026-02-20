import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
  type WheelEvent as ReactWheelEvent,
} from "react";
import clsx from "clsx";
import { gridToIso } from "./layout";
import type { ForumAlert, ForumEntity, ForumMapLayout, ForumMarker, ForumRoom, ForumTask } from "./types";
import type { ForumMapState } from "./state";
import "../styles/forum-map.css";

export type ForumCanvasProps = {
  state: ForumMapState;
  selectedEntityId?: string | null;
  focusEntityId?: string | null;
  onSelectEntity?: (entityId: string | null) => void;
};

type Viewport = {
  offsetX: number;
  offsetY: number;
  scale: number;
};

type PointerState = {
  x: number;
  y: number;
};

const ENTITY_COLORS: Record<ForumEntity["type"], string> = {
  unit: "#C9A66B",
  structure: "#D4A843",
  commis: "#9e7c5a",
  task_node: "#5D9B4A",
};

const DESK_WIDTH = 40;
const DESK_HEIGHT = 22;
const DESK_HIT_PADDING = 8;

const ALERT_COLORS: Record<ForumAlert["level"], string> = {
  L0: "#C9A66B",
  L1: "#D4A843",
  L2: "#D4885A",
  L3: "#C45040",
};

export function ForumCanvas({
  state,
  selectedEntityId,
  focusEntityId,
  onSelectEntity,
}: ForumCanvasProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const viewportRef = useRef<Viewport>({ offsetX: 0, offsetY: 0, scale: 1 });
  const sizeRef = useRef({ width: 0, height: 0, dpr: 1 });
  const pointersRef = useRef<Map<number, PointerState>>(new Map());
  const dragRef = useRef({ dragging: false, moved: 0, lastX: 0, lastY: 0 });
  const pinchRef = useRef<{ distance: number | null }>({ distance: null });
  const rafRef = useRef<number | null>(null);
  const [hudScale, setHudScale] = useState(1);

  const scheduleDraw = useCallback(() => {
    if (rafRef.current != null) return;
    rafRef.current = requestAnimationFrame(() => {
      rafRef.current = null;
      drawCanvas(canvasRef.current, state, viewportRef.current, sizeRef.current, selectedEntityId);
    });
  }, [state, selectedEntityId]);

  useEffect(() => {
    scheduleDraw();
  }, [scheduleDraw, state, selectedEntityId]);

  useEffect(() => {
    return () => {
      if (rafRef.current != null) {
        cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }
    };
  }, []);

  useEffect(() => {
    const container = containerRef.current;
    const canvas = canvasRef.current;
    if (!container || !canvas) return;

    const resize = () => {
      const rect = container.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      canvas.width = rect.width * dpr;
      canvas.height = rect.height * dpr;
      canvas.style.width = `${rect.width}px`;
      canvas.style.height = `${rect.height}px`;
      sizeRef.current = { width: rect.width, height: rect.height, dpr };
      centerViewport(state.layout, viewportRef.current, rect.width, rect.height);
      setHudScale(viewportRef.current.scale);
      scheduleDraw();
    };

    const observer = new ResizeObserver(() => resize());
    observer.observe(container);
    resize();

    return () => observer.disconnect();
  // Stable key: only re-center when the grid dimensions actually change,
  // not on every object reference churn from the 2s poll.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scheduleDraw, state.layout.grid.cols, state.layout.grid.rows]);

  type PointerLike = { clientX: number; clientY: number; currentTarget: EventTarget & HTMLElement };

  const getPointerPosition = (event: PointerLike) => {
    const rect = event.currentTarget.getBoundingClientRect();
    return { x: event.clientX - rect.left, y: event.clientY - rect.top };
  };

  const onPointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    const point = getPointerPosition(event);
    (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
    pointersRef.current.set(event.pointerId, point);
    dragRef.current.dragging = true;
    dragRef.current.moved = 0;
    dragRef.current.lastX = point.x;
    dragRef.current.lastY = point.y;
  };

  const onPointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!dragRef.current.dragging) return;
    const point = getPointerPosition(event);
    pointersRef.current.set(event.pointerId, point);

    if (pointersRef.current.size === 2) {
      const points = Array.from(pointersRef.current.values());
      const distance = Math.hypot(points[0].x - points[1].x, points[0].y - points[1].y);
      if (pinchRef.current.distance != null) {
        const delta = distance / pinchRef.current.distance;
        const centerX = (points[0].x + points[1].x) / 2;
        const centerY = (points[0].y + points[1].y) / 2;
        updateZoom(delta, centerX, centerY);
      }
      pinchRef.current.distance = distance;
      scheduleDraw();
      return;
    }

    const dx = point.x - dragRef.current.lastX;
    const dy = point.y - dragRef.current.lastY;
    dragRef.current.lastX = point.x;
    dragRef.current.lastY = point.y;
    dragRef.current.moved += Math.hypot(dx, dy);

    viewportRef.current.offsetX += dx;
    viewportRef.current.offsetY += dy;
    scheduleDraw();
  };

  const onPointerUp = (event: ReactPointerEvent<HTMLDivElement>) => {
    try {
      event.currentTarget.releasePointerCapture(event.pointerId);
    } catch {
      // Ignore if pointer capture was not set.
    }
    pointersRef.current.delete(event.pointerId);
    if (pointersRef.current.size < 2) {
      pinchRef.current.distance = null;
    }
    const moved = dragRef.current.moved;
    dragRef.current.dragging = false;

    if (moved < 6) {
      const point = getPointerPosition(event);
      const hit: ForumEntity | null = pickEntityAtPoint(state, viewportRef.current, point.x, point.y);
      onSelectEntity?.(hit ? hit.id : null);
    }
  };

  const onWheel = (event: ReactWheelEvent<HTMLDivElement>) => {
    event.preventDefault();
    const point = getPointerPosition(event);
    const delta = event.deltaY > 0 ? 0.92 : 1.08;
    updateZoom(delta, point.x, point.y);
    scheduleDraw();
  };

  const updateZoom = (delta: number, localX: number, localY: number) => {
    const viewport = viewportRef.current;
    const nextScale = Math.min(2.4, Math.max(0.5, viewport.scale * delta));
    const scaleRatio = nextScale / viewport.scale;

    viewport.offsetX = localX - (localX - viewport.offsetX) * scaleRatio;
    viewport.offsetY = localY - (localY - viewport.offsetY) * scaleRatio;
    viewport.scale = nextScale;
    setHudScale(nextScale);
  };

  useEffect(() => {
    if (!focusEntityId) return;
    const entity = state.entities.get(focusEntityId);
    if (!entity) return;
    const { width, height } = sizeRef.current;
    if (!width || !height) return;
    const iso = gridToIso(entity.position, state.layout);
    viewportRef.current.offsetX = width / 2 - iso.x * viewportRef.current.scale;
    viewportRef.current.offsetY = height / 2 - iso.y * viewportRef.current.scale;
    scheduleDraw();
  }, [focusEntityId, scheduleDraw, state.layout, state.entities]);

  return (
    <div
      ref={containerRef}
      className={clsx("forum-map-canvas")}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerUp}
      onWheel={onWheel}
    >
      <canvas ref={canvasRef} />
      <div className="forum-map-hud">
        <div className="forum-map-hud-row">
          <span>Zoom</span>
          <strong>{hudScale.toFixed(2)}x</strong>
        </div>
      </div>
    </div>
  );
}

function centerViewport(layout: ForumMapLayout, viewport: Viewport, width: number, height: number) {
  const centerGrid = {
    col: Math.round(layout.grid.cols / 2),
    row: Math.round(layout.grid.rows / 2),
  };
  const centerIso = gridToIso(centerGrid, layout);
  viewport.scale = 1;
  viewport.offsetX = width / 2 - centerIso.x;
  viewport.offsetY = height / 2 - centerIso.y;
}

function drawCanvas(
  canvas: HTMLCanvasElement | null,
  state: ForumMapState,
  viewport: Viewport,
  size: { width: number; height: number; dpr: number },
  selectedEntityId?: string | null,
) {
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;

  ctx.setTransform(size.dpr, 0, 0, size.dpr, 0, 0);
  ctx.clearRect(0, 0, size.width, size.height);

  ctx.fillStyle = "#120B09";
  ctx.fillRect(0, 0, size.width, size.height);

  ctx.save();
  ctx.translate(viewport.offsetX, viewport.offsetY);
  ctx.scale(viewport.scale, viewport.scale);

  drawRooms(ctx, state.rooms, state.entities, state.layout);
  drawGrid(ctx, state.layout);
  drawEntities(ctx, state.entities, state.layout, selectedEntityId ?? null, viewport.scale);
  drawTasks(ctx, state.tasks, state.entities, state.rooms, state.layout);
  drawAlerts(ctx, state.alerts, state.entities, state.rooms, state.layout);
  drawMarkers(ctx, state.markers, state.layout, state.now);

  ctx.restore();
}

function drawGrid(ctx: CanvasRenderingContext2D, layout: ForumMapLayout) {
  ctx.strokeStyle = "rgba(243, 234, 217, 0.05)";
  ctx.lineWidth = 1;
  for (let col = 0; col < layout.grid.cols; col += 1) {
    const start = gridToIso({ col, row: 0 }, layout);
    const end = gridToIso({ col, row: layout.grid.rows }, layout);
    ctx.beginPath();
    ctx.moveTo(start.x, start.y);
    ctx.lineTo(end.x, end.y);
    ctx.stroke();
  }
  for (let row = 0; row < layout.grid.rows; row += 1) {
    const start = gridToIso({ col: 0, row }, layout);
    const end = gridToIso({ col: layout.grid.cols, row }, layout);
    ctx.beginPath();
    ctx.moveTo(start.x, start.y);
    ctx.lineTo(end.x, end.y);
    ctx.stroke();
  }
}

function drawRooms(
  ctx: CanvasRenderingContext2D,
  rooms: Map<string, ForumRoom>,
  entities: Map<string, ForumEntity>,
  layout: ForumMapLayout,
) {
  // Precompute entity count per room
  const entityCountByRoom = new Map<string, number>();
  entities.forEach((entity) => {
    entityCountByRoom.set(entity.roomId, (entityCountByRoom.get(entity.roomId) ?? 0) + 1);
  });

  let index = 0;
  rooms.forEach((room) => {
    const corners = [
      gridToIso({ col: room.bounds.minCol, row: room.bounds.minRow }, layout),
      gridToIso({ col: room.bounds.maxCol + 1, row: room.bounds.minRow }, layout),
      gridToIso({ col: room.bounds.maxCol + 1, row: room.bounds.maxRow + 1 }, layout),
      gridToIso({ col: room.bounds.minCol, row: room.bounds.maxRow + 1 }, layout),
    ];
    ctx.beginPath();
    ctx.moveTo(corners[0].x, corners[0].y);
    ctx.lineTo(corners[1].x, corners[1].y);
    ctx.lineTo(corners[2].x, corners[2].y);
    ctx.lineTo(corners[3].x, corners[3].y);
    ctx.closePath();
    ctx.fillStyle = index % 2 === 0 ? "rgba(42, 36, 24, 0.35)" : "rgba(33, 28, 21, 0.35)";
    ctx.fill();
    ctx.strokeStyle = "rgba(201, 166, 107, 0.18)";
    ctx.lineWidth = 2;
    ctx.stroke();

    // Room header label: "name (count)" drawn near the top edge of the room
    const roomName = room.name || room.id;
    const entityCount = entityCountByRoom.get(room.id) ?? 0;
    const headerLabel = entityCount > 0 ? `${roomName} (${entityCount})` : roomName;
    // Position label between the top-left and top-right corners
    const headerX = (corners[0].x + corners[1].x) / 2;
    const headerY = (corners[0].y + corners[1].y) / 2 + 14;
    ctx.fillStyle = "rgba(243, 234, 217, 0.85)";
    ctx.font = "bold 13px \"Space Grotesk\", \"Helvetica Neue\", sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(headerLabel, headerX, headerY);
    index += 1;
  });
}

function drawEntities(
  ctx: CanvasRenderingContext2D,
  entities: Map<string, ForumEntity>,
  layout: ForumMapLayout,
  selectedEntityId: string | null,
  viewportScale: number,
) {
  const showLabels = viewportScale > 0.6;

  entities.forEach((entity) => {
    const iso = gridToIso(entity.position, layout);
    const isDisabled = entity.status === "disabled";
    const isActive = entity.status === "working" || entity.status === "moving";
    const color = isDisabled ? "rgba(138, 122, 100, 0.35)" : ENTITY_COLORS[entity.type] || "#9e7c5a";
    const deskX = iso.x - DESK_WIDTH / 2;
    const deskY = iso.y - DESK_HEIGHT / 2;

    // Pulsing ring for active entities — a larger rect at low opacity, no shadow overhead
    if (isActive) {
      const ringPad = 6;
      ctx.fillStyle = "rgba(74, 222, 128, 0.3)";
      ctx.strokeStyle = "rgba(74, 222, 128, 0.45)";
      ctx.lineWidth = 1.5;
      beginRoundedRect(
        ctx,
        deskX - ringPad,
        deskY - ringPad,
        DESK_WIDTH + ringPad * 2,
        DESK_HEIGHT + ringPad * 2,
        8,
      );
      ctx.fill();
      ctx.stroke();
    }

    ctx.fillStyle = isActive ? lightenColor(color) : color;
    ctx.strokeStyle = isActive ? "rgba(74, 222, 128, 0.6)" : "rgba(18, 11, 9, 0.7)";
    ctx.lineWidth = isActive ? 1.5 : 2;
    beginRoundedRect(ctx, deskX, deskY, DESK_WIDTH, DESK_HEIGHT, 4);
    ctx.fill();
    ctx.stroke();

    if (showLabels) {
      const rawLabel = entity.label ?? entity.id;
      // Truncate to ~12 chars to keep labels tidy
      const label = rawLabel.length > 12 ? rawLabel.slice(0, 11) + "…" : rawLabel;
      const subtitle = typeof entity.meta?.subtitle === "string" ? (entity.meta.subtitle as string) : null;
      ctx.fillStyle = "rgba(243, 234, 217, 0.85)";
      ctx.font = "9px \"Space Grotesk\", \"Helvetica Neue\", sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(label, iso.x, iso.y + DESK_HEIGHT / 2 + 10);

      if (subtitle) {
        ctx.fillStyle = "rgba(181, 164, 142, 0.7)";
        ctx.font = "8px \"Space Grotesk\", \"Helvetica Neue\", sans-serif";
        ctx.fillText(subtitle, iso.x, iso.y + DESK_HEIGHT / 2 + 21);
      }
    }

    if (selectedEntityId === entity.id) {
      ctx.strokeStyle = "rgba(201, 166, 107, 0.7)";
      ctx.lineWidth = 2;
      beginRoundedRect(ctx, deskX - 4, deskY - 4, DESK_WIDTH + 8, DESK_HEIGHT + 8, 6);
      ctx.stroke();
    }
  });
}

function drawTasks(
  ctx: CanvasRenderingContext2D,
  tasks: Map<string, ForumTask>,
  entities: Map<string, ForumEntity>,
  rooms: Map<string, ForumRoom>,
  layout: ForumMapLayout,
) {
  tasks.forEach((task) => {
    const position = resolveTaskPosition(task, entities, rooms, layout);
    ctx.save();
    ctx.translate(position.x, position.y - 12);
    ctx.rotate(Math.PI / 4);
    ctx.fillStyle = task.status === "failed" ? "#C45040" : "#5D9B4A";
    ctx.fillRect(-4, -4, 8, 8);
    ctx.restore();
  });
}

function drawAlerts(
  ctx: CanvasRenderingContext2D,
  alerts: Map<string, ForumAlert>,
  entities: Map<string, ForumEntity>,
  rooms: Map<string, ForumRoom>,
  layout: ForumMapLayout,
) {
  alerts.forEach((alert) => {
    const position = resolveAlertPosition(alert, entities, rooms, layout);
    ctx.strokeStyle = ALERT_COLORS[alert.level];
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(position.x, position.y, 16, 0, Math.PI * 2);
    ctx.stroke();
  });
}

function drawMarkers(
  ctx: CanvasRenderingContext2D,
  markers: Map<string, ForumMarker>,
  layout: ForumMapLayout,
  now: number,
) {
  markers.forEach((marker) => {
    if (marker.expiresAt && marker.expiresAt < now) {
      return;
    }
    const iso = gridToIso(marker.position, layout);
    ctx.strokeStyle = "rgba(243, 234, 217, 0.6)";
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(iso.x - 6, iso.y - 6);
    ctx.lineTo(iso.x + 6, iso.y + 6);
    ctx.moveTo(iso.x + 6, iso.y - 6);
    ctx.lineTo(iso.x - 6, iso.y + 6);
    ctx.stroke();
  });
}

function resolveTaskPosition(
  task: ForumTask,
  entities: Map<string, ForumEntity>,
  rooms: Map<string, ForumRoom>,
  layout: ForumMapLayout,
) {
  const entity = task.entityId ? entities.get(task.entityId) : null;
  if (entity) return gridToIso(entity.position, layout);
  const room = rooms.get(task.roomId);
  if (room) return gridToIso(room.center, layout);
  return { x: 0, y: 0 };
}

function resolveAlertPosition(
  alert: ForumAlert,
  entities: Map<string, ForumEntity>,
  rooms: Map<string, ForumRoom>,
  layout: ForumMapLayout,
) {
  const entity = alert.entityId ? entities.get(alert.entityId) : null;
  if (entity) return gridToIso(entity.position, layout);
  const room = rooms.get(alert.roomId);
  if (room) return gridToIso(room.center, layout);
  return { x: 0, y: 0 };
}

function pickEntityAtPoint(
  state: ForumMapState,
  viewport: Viewport,
  x: number,
  y: number,
): ForumEntity | null {
  let closest: ForumEntity | null = null;
  let closestDistance = Number.POSITIVE_INFINITY;
  state.entities.forEach((entity) => {
    const iso = gridToIso(entity.position, state.layout);
    const screenX = iso.x * viewport.scale + viewport.offsetX;
    const screenY = iso.y * viewport.scale + viewport.offsetY;
    const halfWidth = (DESK_WIDTH / 2 + DESK_HIT_PADDING) * viewport.scale;
    const halfHeight = (DESK_HEIGHT / 2 + DESK_HIT_PADDING) * viewport.scale;
    const within =
      x >= screenX - halfWidth &&
      x <= screenX + halfWidth &&
      y >= screenY - halfHeight &&
      y <= screenY + halfHeight;
    const distance = Math.hypot(screenX - x, screenY - y);
    if (within && distance < closestDistance) {
      closest = entity;
      closestDistance = distance;
    }
  });
  return closest ?? null;
}

/**
 * Lighten a hex color by blending it toward white.
 * Used to make active entity desks pop slightly brighter than their base color.
 */
function lightenColor(hex: string): string {
  const clean = hex.replace("#", "");
  if (clean.length !== 6) return hex;
  const r = parseInt(clean.slice(0, 2), 16);
  const g = parseInt(clean.slice(2, 4), 16);
  const b = parseInt(clean.slice(4, 6), 16);
  const factor = 0.35;
  const lr = Math.round(r + (255 - r) * factor);
  const lg = Math.round(g + (255 - g) * factor);
  const lb = Math.round(b + (255 - b) * factor);
  return `rgb(${lr}, ${lg}, ${lb})`;
}

function beginRoundedRect(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  width: number,
  height: number,
  radius: number,
) {
  ctx.beginPath();
  const roundRect = (ctx as { roundRect?: (...args: any[]) => void }).roundRect;
  if (typeof roundRect === "function") {
    roundRect.call(ctx, x, y, width, height, radius);
    return;
  }
  const r = Math.min(radius, width / 2, height / 2);
  ctx.moveTo(x + r, y);
  ctx.lineTo(x + width - r, y);
  ctx.quadraticCurveTo(x + width, y, x + width, y + r);
  ctx.lineTo(x + width, y + height - r);
  ctx.quadraticCurveTo(x + width, y + height, x + width - r, y + height);
  ctx.lineTo(x + r, y + height);
  ctx.quadraticCurveTo(x, y + height, x, y + height - r);
  ctx.lineTo(x, y + r);
  ctx.quadraticCurveTo(x, y, x + r, y);
}
