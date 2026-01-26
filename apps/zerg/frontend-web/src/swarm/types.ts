export type SwarmId = string;

export type SwarmPoint = {
  x: number;
  y: number;
};

export type SwarmGridPoint = {
  col: number;
  row: number;
};

export type SwarmBounds = {
  minCol: number;
  minRow: number;
  maxCol: number;
  maxRow: number;
};

export type SwarmMapLayout = {
  id: SwarmId;
  name: string;
  grid: {
    cols: number;
    rows: number;
  };
  tile: {
    width: number;
    height: number;
  };
  origin: SwarmPoint;
};

export type SwarmWorkspace = {
  id: SwarmId;
  name: string;
  repoGroups: SwarmId[];
};

export type SwarmRepoGroup = {
  id: SwarmId;
  name: string;
  workspaceId: SwarmId;
  roomIds: SwarmId[];
};

export type SwarmRoom = {
  id: SwarmId;
  name: string;
  workspaceId: SwarmId;
  repoGroupId: SwarmId;
  bounds: SwarmBounds;
  center: SwarmGridPoint;
};

export type SwarmEntityType = "unit" | "structure" | "worker" | "task_node";

export type SwarmEntityStatus = "idle" | "moving" | "working" | "disabled";

export type SwarmEntity = {
  id: SwarmId;
  type: SwarmEntityType;
  roomId: SwarmId;
  position: SwarmGridPoint;
  status: SwarmEntityStatus;
  label?: string;
  meta?: Record<string, unknown>;
};

export type SwarmTaskStatus = "queued" | "running" | "waiting" | "success" | "failed";

export type SwarmTask = {
  id: SwarmId;
  title: string;
  status: SwarmTaskStatus;
  roomId: SwarmId;
  entityId?: SwarmId;
  workerId?: SwarmId;
  progress: number;
  createdAt: number;
  updatedAt: number;
};

export type SwarmWorkerStatus = "idle" | "busy" | "offline";

export type SwarmWorker = {
  id: SwarmId;
  name: string;
  status: SwarmWorkerStatus;
  roomId: SwarmId;
  entityId?: SwarmId;
};

export type SwarmAlertLevel = "L0" | "L1" | "L2" | "L3";

export type SwarmAlert = {
  id: SwarmId;
  level: SwarmAlertLevel;
  message: string;
  roomId: SwarmId;
  taskId?: SwarmId;
  entityId?: SwarmId;
  createdAt: number;
};

export type SwarmMarkerType = "route" | "focus" | "ping" | "target";

export type SwarmMarker = {
  id: SwarmId;
  type: SwarmMarkerType;
  roomId: SwarmId;
  position: SwarmGridPoint;
  label?: string;
  createdAt: number;
  expiresAt?: number;
};

export type SwarmReplayEventType =
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

export type SwarmReplayEventBase = {
  id: SwarmId;
  t: number;
  seq: number;
  type: SwarmReplayEventType;
};

export type SwarmReplayEvent =
  | (SwarmReplayEventBase & {
      type: "layout.set";
      layout: SwarmMapLayout;
    })
  | (SwarmReplayEventBase & {
      type: "room.add";
      room: SwarmRoom;
    })
  | (SwarmReplayEventBase & {
      type: "entity.add";
      entity: SwarmEntity;
    })
  | (SwarmReplayEventBase & {
      type: "entity.move";
      entityId: SwarmId;
      roomId: SwarmId;
      position: SwarmGridPoint;
      status?: SwarmEntityStatus;
    })
  | (SwarmReplayEventBase & {
      type: "entity.remove";
      entityId: SwarmId;
    })
  | (SwarmReplayEventBase & {
      type: "task.add";
      task: SwarmTask;
    })
  | (SwarmReplayEventBase & {
      type: "task.update";
      taskId: SwarmId;
      status?: SwarmTaskStatus;
      progress?: number;
      workerId?: SwarmId;
      entityId?: SwarmId;
      title?: string;
      updatedAt: number;
    })
  | (SwarmReplayEventBase & {
      type: "task.resolve";
      taskId: SwarmId;
      status: "success" | "failed";
      progress: number;
      updatedAt: number;
    })
  | (SwarmReplayEventBase & {
      type: "alert.raise";
      alert: SwarmAlert;
    })
  | (SwarmReplayEventBase & {
      type: "alert.clear";
      alertId: SwarmId;
    })
  | (SwarmReplayEventBase & {
      type: "marker.add";
      marker: SwarmMarker;
    })
  | (SwarmReplayEventBase & {
      type: "marker.clear";
      markerId: SwarmId;
    })
  | (SwarmReplayEventBase & {
      type: "worker.add";
      worker: SwarmWorker;
    })
  | (SwarmReplayEventBase & {
      type: "worker.update";
      workerId: SwarmId;
      status?: SwarmWorkerStatus;
      entityId?: SwarmId;
    });

export type SwarmReplayScenario = {
  seed: string;
  createdAt: number;
  durationMs: number;
  tickMs: number;
  layout: SwarmMapLayout;
  workspaces: SwarmWorkspace[];
  repoGroups: SwarmRepoGroup[];
  rooms: SwarmRoom[];
  events: SwarmReplayEvent[];
};

export type SwarmReplayConfig = {
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
