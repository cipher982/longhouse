export type ForumId = string;

export type ForumPoint = {
  x: number;
  y: number;
};

export type ForumGridPoint = {
  col: number;
  row: number;
};

export type ForumBounds = {
  minCol: number;
  minRow: number;
  maxCol: number;
  maxRow: number;
};

export type ForumMapLayout = {
  id: ForumId;
  name: string;
  grid: {
    cols: number;
    rows: number;
  };
  tile: {
    width: number;
    height: number;
  };
  origin: ForumPoint;
};

export type ForumWorkspace = {
  id: ForumId;
  name: string;
  repoGroups: ForumId[];
};

export type ForumRepoGroup = {
  id: ForumId;
  name: string;
  workspaceId: ForumId;
  roomIds: ForumId[];
};

export type ForumRoom = {
  id: ForumId;
  name: string;
  workspaceId: ForumId;
  repoGroupId: ForumId;
  bounds: ForumBounds;
  center: ForumGridPoint;
};

export type ForumEntityType = "unit" | "structure" | "worker" | "task_node";

export type ForumEntityStatus = "idle" | "moving" | "working" | "disabled";

export type ForumEntity = {
  id: ForumId;
  type: ForumEntityType;
  roomId: ForumId;
  position: ForumGridPoint;
  status: ForumEntityStatus;
  label?: string;
  meta?: Record<string, unknown>;
};

export type ForumTaskStatus = "queued" | "running" | "waiting" | "success" | "failed";

export type ForumTask = {
  id: ForumId;
  title: string;
  status: ForumTaskStatus;
  roomId: ForumId;
  entityId?: ForumId;
  workerId?: ForumId;
  progress: number;
  createdAt: number;
  updatedAt: number;
};

export type ForumWorkerStatus = "idle" | "busy" | "offline";

export type ForumWorker = {
  id: ForumId;
  name: string;
  status: ForumWorkerStatus;
  roomId: ForumId;
  entityId?: ForumId;
};

export type ForumAlertLevel = "L0" | "L1" | "L2" | "L3";

export type ForumAlert = {
  id: ForumId;
  level: ForumAlertLevel;
  message: string;
  roomId: ForumId;
  taskId?: ForumId;
  entityId?: ForumId;
  createdAt: number;
};

export type ForumMarkerType = "route" | "focus" | "ping" | "target";

export type ForumMarker = {
  id: ForumId;
  type: ForumMarkerType;
  roomId: ForumId;
  position: ForumGridPoint;
  label?: string;
  createdAt: number;
  expiresAt?: number;
};

export type ForumReplayEventType =
  | "layout.set"
  | "room.add"
  | "entity.add"
  | "entity.move"
  | "entity.remove"
  | "task.add"
  | "task.update"
  | "task.resolve"
  | "alert.raise"
  | "alert.clear"
  | "marker.add"
  | "marker.clear"
  | "worker.add"
  | "worker.update";

export type ForumReplayEventBase = {
  id: ForumId;
  t: number;
  seq: number;
  type: ForumReplayEventType;
};

export type ForumReplayEvent =
  | (ForumReplayEventBase & {
      type: "layout.set";
      layout: ForumMapLayout;
    })
  | (ForumReplayEventBase & {
      type: "room.add";
      room: ForumRoom;
    })
  | (ForumReplayEventBase & {
      type: "entity.add";
      entity: ForumEntity;
    })
  | (ForumReplayEventBase & {
      type: "entity.move";
      entityId: ForumId;
      roomId: ForumId;
      position: ForumGridPoint;
      status?: ForumEntityStatus;
    })
  | (ForumReplayEventBase & {
      type: "entity.remove";
      entityId: ForumId;
    })
  | (ForumReplayEventBase & {
      type: "task.add";
      task: ForumTask;
    })
  | (ForumReplayEventBase & {
      type: "task.update";
      taskId: ForumId;
      status?: ForumTaskStatus;
      progress?: number;
      workerId?: ForumId;
      entityId?: ForumId;
      title?: string;
      updatedAt: number;
    })
  | (ForumReplayEventBase & {
      type: "task.resolve";
      taskId: ForumId;
      status: "success" | "failed";
      progress: number;
      updatedAt: number;
    })
  | (ForumReplayEventBase & {
      type: "alert.raise";
      alert: ForumAlert;
    })
  | (ForumReplayEventBase & {
      type: "alert.clear";
      alertId: ForumId;
    })
  | (ForumReplayEventBase & {
      type: "marker.add";
      marker: ForumMarker;
    })
  | (ForumReplayEventBase & {
      type: "marker.clear";
      markerId: ForumId;
    })
  | (ForumReplayEventBase & {
      type: "worker.add";
      worker: ForumWorker;
    })
  | (ForumReplayEventBase & {
      type: "worker.update";
      workerId: ForumId;
      status?: ForumWorkerStatus;
      entityId?: ForumId;
    });

export type ForumReplayEventInput = {
  [K in ForumReplayEvent["type"]]: Omit<Extract<ForumReplayEvent, { type: K }>, "id" | "seq">;
}[ForumReplayEvent["type"]];

export type ForumReplayScenario = {
  seed: string;
  createdAt: number;
  durationMs: number;
  tickMs: number;
  layout: ForumMapLayout;
  workspaces: ForumWorkspace[];
  repoGroups: ForumRepoGroup[];
  rooms: ForumRoom[];
  events: ForumReplayEvent[];
};

export type ForumReplayConfig = {
  seed: string;
  durationMs?: number;
  tickMs?: number;
  roomCount?: number;
  unitsPerRoom?: number;
  tasksPerRoom?: number;
  workersPerRoom?: number;
  repoGroupsPerWorkspace?: number;
  workspaceCount?: number;
};
