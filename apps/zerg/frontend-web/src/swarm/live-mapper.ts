import type { EventMap } from "../jarvis/lib/event-bus";
import type { SwarmMapState } from "./state";
import type {
  SwarmAlert,
  SwarmEntity,
  SwarmReplayEventInput,
  SwarmRoom,
  SwarmTask,
} from "./types";

const getDefaultRoom = (state: SwarmMapState): SwarmRoom | null => {
  const iterator = state.rooms.values().next();
  return iterator.done ? null : iterator.value;
};

const positionForId = (id: string, bounds: SwarmRoom["bounds"]) => {
  const hash = Array.from(id).reduce((acc, char) => acc + char.charCodeAt(0), 0);
  const spanCol = Math.max(1, bounds.maxCol - bounds.minCol);
  const spanRow = Math.max(1, bounds.maxRow - bounds.minRow);
  return {
    col: bounds.minCol + (hash % spanCol),
    row: bounds.minRow + ((hash * 7) % spanRow),
  };
};

const ensureTaskEntity = (
  state: SwarmMapState,
  room: SwarmRoom,
  taskId: string,
): { entityId: string; events: SwarmReplayEventInput[] } => {
  const entityId = `task-node-${taskId}`;
  if (state.entities.has(entityId)) {
    return { entityId, events: [] };
  }
  const entity: SwarmEntity = {
    id: entityId,
    type: "task_node",
    roomId: room.id,
    position: positionForId(entityId, room.bounds),
    status: "working",
    label: `Task Node ${taskId}`,
  };
  return { entityId, events: [{ t: 0, type: "entity.add", entity }] };
};

export function mapSupervisorStarted(
  state: SwarmMapState,
  payload: EventMap["supervisor:started"],
): SwarmReplayEventInput[] {
  const room = getDefaultRoom(state);
  if (!room) return [];

  const taskId = `run-${payload.runId}`;
  const events: SwarmReplayEventInput[] = [];
  const { entityId, events: entityEvents } = ensureTaskEntity(state, room, taskId);
  events.push(...entityEvents);

  if (!state.tasks.has(taskId)) {
    const task: SwarmTask = {
      id: taskId,
      title: payload.task,
      status: "running",
      roomId: room.id,
      entityId,
      progress: 0,
      createdAt: payload.timestamp,
      updatedAt: payload.timestamp,
    };
    events.push({ t: payload.timestamp, type: "task.add", task });
  }

  return events.map((event) => ({ ...event, t: payload.timestamp }));
}

export function mapWorkerSpawned(
  state: SwarmMapState,
  payload: EventMap["supervisor:worker_spawned"],
): SwarmReplayEventInput[] {
  const room = getDefaultRoom(state);
  if (!room) return [];

  const workerId = `worker-${payload.jobId}`;
  const entityId = `worker-entity-${payload.jobId}`;
  const events: SwarmReplayEventInput[] = [];

  if (!state.entities.has(entityId)) {
    const entity: SwarmEntity = {
      id: entityId,
      type: "worker",
      roomId: room.id,
      position: positionForId(entityId, room.bounds),
      status: "working",
      label: `Worker ${payload.jobId}`,
    };
    events.push({ t: payload.timestamp, type: "entity.add", entity });
  }

  if (!state.workers.has(workerId)) {
    events.push({
      t: payload.timestamp,
      type: "worker.add",
      worker: {
        id: workerId,
        name: `Worker ${payload.jobId}`,
        status: "busy",
        roomId: room.id,
        entityId,
      },
    });
  }

  const taskId = `job-${payload.jobId}`;
  if (!state.tasks.has(taskId)) {
    const task: SwarmTask = {
      id: taskId,
      title: payload.task,
      status: "running",
      roomId: room.id,
      entityId,
      workerId,
      progress: 0,
      createdAt: payload.timestamp,
      updatedAt: payload.timestamp,
    };
    events.push({ t: payload.timestamp, type: "task.add", task });
  }

  return events;
}

export function mapWorkerComplete(
  state: SwarmMapState,
  payload: EventMap["supervisor:worker_complete"],
): SwarmReplayEventInput[] {
  const events: SwarmReplayEventInput[] = [];
  const workerId = `worker-${payload.jobId}`;
  if (state.workers.has(workerId)) {
    events.push({
      t: payload.timestamp,
      type: "worker.update",
      workerId,
      status: payload.status === "success" ? "idle" : "offline",
    });
  }

  const taskId = `job-${payload.jobId}`;
  if (state.tasks.has(taskId)) {
    events.push({
      t: payload.timestamp,
      type: "task.resolve",
      taskId,
      status: payload.status === "success" ? "success" : "failed",
      progress: 1,
      updatedAt: payload.timestamp,
    });
  }

  if (payload.status !== "success") {
    const room = getDefaultRoom(state);
    if (room) {
      const alert: SwarmAlert = {
        id: `alert-worker-${payload.jobId}-${payload.timestamp}`,
        level: "L2",
        message: `Worker ${payload.jobId} failed`,
        roomId: room.id,
        createdAt: payload.timestamp,
      };
      events.push({ t: payload.timestamp, type: "alert.raise", alert });
    }
  }

  return events;
}

export function mapSupervisorComplete(
  state: SwarmMapState,
  payload: EventMap["supervisor:complete"],
): SwarmReplayEventInput[] {
  const taskId = `run-${payload.runId}`;
  if (!state.tasks.has(taskId)) return [];

  return [
    {
      t: payload.timestamp,
      type: "task.resolve",
      taskId,
      status: payload.status === "success" ? "success" : "failed",
      progress: 1,
      updatedAt: payload.timestamp,
    },
  ];
}

export function mapWorkerToolFailed(
  state: SwarmMapState,
  payload: EventMap["worker:tool_failed"],
): SwarmReplayEventInput[] {
  const room = getDefaultRoom(state);
  if (!room) return [];

  const alert: SwarmAlert = {
    id: `alert-tool-${payload.toolCallId}-${payload.timestamp}`,
    level: "L1",
    message: `${payload.toolName} failed`,
    roomId: room.id,
    createdAt: payload.timestamp,
  };

  return [{ t: payload.timestamp, type: "alert.raise", alert }];
}
