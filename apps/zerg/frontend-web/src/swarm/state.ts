import type {
  SwarmAlert,
  SwarmEntity,
  SwarmMapLayout,
  SwarmMarker,
  SwarmRepoGroup,
  SwarmReplayEvent,
  SwarmRoom,
  SwarmTask,
  SwarmWorker,
  SwarmWorkspace,
} from "./types";

export type SwarmMapState = {
  layout: SwarmMapLayout;
  workspaces: Map<string, SwarmWorkspace>;
  repoGroups: Map<string, SwarmRepoGroup>;
  rooms: Map<string, SwarmRoom>;
  entities: Map<string, SwarmEntity>;
  tasks: Map<string, SwarmTask>;
  workers: Map<string, SwarmWorker>;
  alerts: Map<string, SwarmAlert>;
  markers: Map<string, SwarmMarker>;
  appliedEvents: Set<string>;
  now: number;
};

export function createSwarmState(base: {
  layout: SwarmMapLayout;
  workspaces?: SwarmWorkspace[];
  repoGroups?: SwarmRepoGroup[];
  rooms?: SwarmRoom[];
}): SwarmMapState {
  const workspaces = new Map<string, SwarmWorkspace>();
  const repoGroups = new Map<string, SwarmRepoGroup>();
  const rooms = new Map<string, SwarmRoom>();

  for (const workspace of base.workspaces ?? []) {
    workspaces.set(workspace.id, workspace);
  }

  for (const repoGroup of base.repoGroups ?? []) {
    repoGroups.set(repoGroup.id, repoGroup);
  }

  for (const room of base.rooms ?? []) {
    rooms.set(room.id, room);
  }

  return {
    layout: base.layout,
    workspaces,
    repoGroups,
    rooms,
    entities: new Map(),
    tasks: new Map(),
    workers: new Map(),
    alerts: new Map(),
    markers: new Map(),
    appliedEvents: new Set(),
    now: 0,
  };
}

export function applySwarmEvents(state: SwarmMapState, events: SwarmReplayEvent[]): SwarmMapState {
  for (const event of events) {
    applySwarmEvent(state, event);
  }
  return state;
}

export function applySwarmEvent(state: SwarmMapState, event: SwarmReplayEvent): void {
  if (state.appliedEvents.has(event.id)) {
    return;
  }

  state.appliedEvents.add(event.id);
  state.now = Math.max(state.now, event.t);

  switch (event.type) {
    case "layout.set": {
      state.layout = event.layout;
      return;
    }
    case "room.add": {
      state.rooms.set(event.room.id, event.room);
      const repoGroup = state.repoGroups.get(event.room.repoGroupId);
      if (repoGroup && !repoGroup.roomIds.includes(event.room.id)) {
        repoGroup.roomIds = [...repoGroup.roomIds, event.room.id];
      }
      return;
    }
    case "entity.add": {
      state.entities.set(event.entity.id, event.entity);
      return;
    }
    case "entity.move": {
      const existing = state.entities.get(event.entityId);
      if (!existing) return;
      state.entities.set(event.entityId, {
        ...existing,
        roomId: event.roomId,
        position: event.position,
        status: event.status ?? existing.status,
      });
      return;
    }
    case "entity.remove": {
      state.entities.delete(event.entityId);
      return;
    }
    case "task.add": {
      state.tasks.set(event.task.id, event.task);
      return;
    }
    case "task.update": {
      const existing = state.tasks.get(event.taskId);
      if (!existing) return;
      state.tasks.set(event.taskId, {
        ...existing,
        status: event.status ?? existing.status,
        progress: event.progress ?? existing.progress,
        workerId: event.workerId ?? existing.workerId,
        entityId: event.entityId ?? existing.entityId,
        title: event.title ?? existing.title,
        updatedAt: event.updatedAt,
      });
      return;
    }
    case "task.resolve": {
      const existing = state.tasks.get(event.taskId);
      if (!existing) return;
      state.tasks.set(event.taskId, {
        ...existing,
        status: event.status,
        progress: event.progress,
        updatedAt: event.updatedAt,
      });
      return;
    }
    case "alert.raise": {
      state.alerts.set(event.alert.id, event.alert);
      return;
    }
    case "alert.clear": {
      state.alerts.delete(event.alertId);
      return;
    }
    case "marker.add": {
      state.markers.set(event.marker.id, event.marker);
      return;
    }
    case "marker.clear": {
      state.markers.delete(event.markerId);
      return;
    }
    case "worker.add": {
      state.workers.set(event.worker.id, event.worker);
      return;
    }
    case "worker.update": {
      const existing = state.workers.get(event.workerId);
      if (!existing) return;
      state.workers.set(event.workerId, {
        ...existing,
        status: event.status ?? existing.status,
        entityId: event.entityId ?? existing.entityId,
      });
      return;
    }
    default: {
      const _exhaustiveCheck: never = event;
      return _exhaustiveCheck;
    }
  }
}
