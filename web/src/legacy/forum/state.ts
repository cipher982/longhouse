import type {
  ForumAlert,
  ForumEntity,
  ForumMapLayout,
  ForumMarker,
  ForumRepoGroup,
  ForumReplayEvent,
  ForumRoom,
  ForumTask,
  ForumCommis,
  ForumWorkspace,
} from "./types";

export type ForumMapState = {
  layout: ForumMapLayout;
  workspaces: Map<string, ForumWorkspace>;
  repoGroups: Map<string, ForumRepoGroup>;
  rooms: Map<string, ForumRoom>;
  entities: Map<string, ForumEntity>;
  tasks: Map<string, ForumTask>;
  commiss: Map<string, ForumCommis>;
  alerts: Map<string, ForumAlert>;
  markers: Map<string, ForumMarker>;
  appliedEvents: Set<string>;
  now: number;
};

export function createForumState(base: {
  layout: ForumMapLayout;
  workspaces?: ForumWorkspace[];
  repoGroups?: ForumRepoGroup[];
  rooms?: ForumRoom[];
}): ForumMapState {
  const workspaces = new Map<string, ForumWorkspace>();
  const repoGroups = new Map<string, ForumRepoGroup>();
  const rooms = new Map<string, ForumRoom>();

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
    commiss: new Map(),
    alerts: new Map(),
    markers: new Map(),
    appliedEvents: new Set(),
    now: 0,
  };
}

export function applyForumEvents(state: ForumMapState, events: ForumReplayEvent[]): ForumMapState {
  for (const event of events) {
    applyForumEvent(state, event);
  }
  return state;
}

export function applyForumEvent(state: ForumMapState, event: ForumReplayEvent): void {
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
        commisId: event.commisId ?? existing.commisId,
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
    case "commis.add": {
      state.commiss.set(event.commis.id, event.commis);
      return;
    }
    case "commis.update": {
      const existing = state.commiss.get(event.commisId);
      if (!existing) return;
      state.commiss.set(event.commisId, {
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
