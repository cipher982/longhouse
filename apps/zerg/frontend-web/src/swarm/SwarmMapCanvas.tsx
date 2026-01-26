import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
  type WheelEvent as ReactWheelEvent,
} from "react";
import clsx from "clsx";
import { gridToIso } from "./layout";
import type { SwarmAlert, SwarmEntity, SwarmMapLayout, SwarmMarker, SwarmRoom, SwarmTask } from "./types";
import type { SwarmMapState } from "./state";
import "../styles/swarm-map.css";

export type SwarmMapCanvasProps = {
  state: SwarmMapState;
  timeMs: number;
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

const ENTITY_COLORS: Record<SwarmEntity["type"], string> = {
  unit: "#5CE0FF",
  structure: "#F7B955",
  worker: "#7B7CFF",
  task_node: "#36EBA8",
};

const ALERT_COLORS: Record<SwarmAlert["level"], string> = {
  L0: "#46E2F2",
  L1: "#F7C055",
  L2: "#F48B4A",
  L3: "#F05454",
};

export function SwarmMapCanvas({
  state,
  timeMs,
  selectedEntityId,
  focusEntityId,
  onSelectEntity,
}: SwarmMapCanvasProps) {
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
  }, [scheduleDraw, state, timeMs, selectedEntityId]);

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
  }, [scheduleDraw, state.layout]);

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
      const hit: SwarmEntity | null = pickEntityAtPoint(state, viewportRef.current, point.x, point.y);
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
  }, [focusEntityId, timeMs, scheduleDraw, state.layout, state.entities]);

  return (
    <div
      ref={containerRef}
      className={clsx("swarm-map-canvas")}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerUp}
      onWheel={onWheel}
    >
      <canvas ref={canvasRef} />
      <div className="swarm-map-hud">
        <div className="swarm-map-hud-row">
          <span>Zoom</span>
          <strong>{hudScale.toFixed(2)}x</strong>
        </div>
        <div className="swarm-map-hud-row">
          <span>t</span>
          <strong>{Math.round(timeMs / 100) / 10}s</strong>
        </div>
      </div>
    </div>
  );
}

function centerViewport(layout: SwarmMapLayout, viewport: Viewport, width: number, height: number) {
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
  state: SwarmMapState,
  viewport: Viewport,
  size: { width: number; height: number; dpr: number },
  selectedEntityId?: string | null,
) {
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;

  ctx.setTransform(size.dpr, 0, 0, size.dpr, 0, 0);
  ctx.clearRect(0, 0, size.width, size.height);

  ctx.fillStyle = "#0B0E14";
  ctx.fillRect(0, 0, size.width, size.height);

  ctx.save();
  ctx.translate(viewport.offsetX, viewport.offsetY);
  ctx.scale(viewport.scale, viewport.scale);

  drawRooms(ctx, state.rooms, state.layout);
  drawGrid(ctx, state.layout);
  drawEntities(ctx, state.entities, state.layout, selectedEntityId ?? null);
  drawTasks(ctx, state.tasks, state.entities, state.rooms, state.layout);
  drawAlerts(ctx, state.alerts, state.entities, state.rooms, state.layout);
  drawMarkers(ctx, state.markers, state.layout, state.now);

  ctx.restore();
}

function drawGrid(ctx: CanvasRenderingContext2D, layout: SwarmMapLayout) {
  ctx.strokeStyle = "rgba(255, 255, 255, 0.05)";
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

function drawRooms(ctx: CanvasRenderingContext2D, rooms: Map<string, SwarmRoom>, layout: SwarmMapLayout) {
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
    ctx.fillStyle = index % 2 === 0 ? "rgba(33, 72, 99, 0.18)" : "rgba(77, 54, 108, 0.16)";
    ctx.fill();
    ctx.strokeStyle = "rgba(120, 160, 220, 0.18)";
    ctx.lineWidth = 2;
    ctx.stroke();
    index += 1;
  });
}

function drawEntities(
  ctx: CanvasRenderingContext2D,
  entities: Map<string, SwarmEntity>,
  layout: SwarmMapLayout,
  selectedEntityId: string | null,
) {
  entities.forEach((entity) => {
    const iso = gridToIso(entity.position, layout);
    const color = ENTITY_COLORS[entity.type] || "#7B7CFF";
    ctx.beginPath();
    ctx.fillStyle = color;
    ctx.arc(iso.x, iso.y, entity.type === "structure" ? 8 : 6, 0, Math.PI * 2);
    ctx.fill();

    if (selectedEntityId === entity.id) {
      ctx.strokeStyle = "rgba(255, 255, 255, 0.7)";
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(iso.x, iso.y, 12, 0, Math.PI * 2);
      ctx.stroke();
    }
  });
}

function drawTasks(
  ctx: CanvasRenderingContext2D,
  tasks: Map<string, SwarmTask>,
  entities: Map<string, SwarmEntity>,
  rooms: Map<string, SwarmRoom>,
  layout: SwarmMapLayout,
) {
  tasks.forEach((task) => {
    const position = resolveTaskPosition(task, entities, rooms, layout);
    ctx.save();
    ctx.translate(position.x, position.y - 12);
    ctx.rotate(Math.PI / 4);
    ctx.fillStyle = task.status === "failed" ? "#F05454" : "#36EBA8";
    ctx.fillRect(-4, -4, 8, 8);
    ctx.restore();
  });
}

function drawAlerts(
  ctx: CanvasRenderingContext2D,
  alerts: Map<string, SwarmAlert>,
  entities: Map<string, SwarmEntity>,
  rooms: Map<string, SwarmRoom>,
  layout: SwarmMapLayout,
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
  markers: Map<string, SwarmMarker>,
  layout: SwarmMapLayout,
  now: number,
) {
  markers.forEach((marker) => {
    if (marker.expiresAt && marker.expiresAt < now) {
      return;
    }
    const iso = gridToIso(marker.position, layout);
    ctx.strokeStyle = "rgba(255, 255, 255, 0.6)";
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
  task: SwarmTask,
  entities: Map<string, SwarmEntity>,
  rooms: Map<string, SwarmRoom>,
  layout: SwarmMapLayout,
) {
  const entity = task.entityId ? entities.get(task.entityId) : null;
  if (entity) return gridToIso(entity.position, layout);
  const room = rooms.get(task.roomId);
  if (room) return gridToIso(room.center, layout);
  return { x: 0, y: 0 };
}

function resolveAlertPosition(
  alert: SwarmAlert,
  entities: Map<string, SwarmEntity>,
  rooms: Map<string, SwarmRoom>,
  layout: SwarmMapLayout,
) {
  const entity = alert.entityId ? entities.get(alert.entityId) : null;
  if (entity) return gridToIso(entity.position, layout);
  const room = rooms.get(alert.roomId);
  if (room) return gridToIso(room.center, layout);
  return { x: 0, y: 0 };
}

function pickEntityAtPoint(
  state: SwarmMapState,
  viewport: Viewport,
  x: number,
  y: number,
): SwarmEntity | null {
  let closest: SwarmEntity | null = null;
  let closestDistance = Number.POSITIVE_INFINITY;
  state.entities.forEach((entity) => {
    const iso = gridToIso(entity.position, state.layout);
    const screenX = iso.x * viewport.scale + viewport.offsetX;
    const screenY = iso.y * viewport.scale + viewport.offsetY;
    const distance = Math.hypot(screenX - x, screenY - y);
    if (distance <= 18 && distance < closestDistance) {
      closest = entity;
      closestDistance = distance;
    }
  });
  return closest ?? null;
}
