import type {
  ForumAlert,
  ForumEntity,
  ForumMarker,
  ForumReplayConfig,
  ForumReplayEvent,
  ForumReplayEventInput,
  ForumReplayScenario,
  ForumRepoGroup,
  ForumRoom,
  ForumTask,
  ForumWorker,
  ForumWorkspace,
} from "./types";
import { applyForumEvents, createForumState, type ForumMapState } from "./state";
import type { ForumBounds, ForumGridPoint, ForumMapLayout } from "./types";

export type ForumRng = {
  next: () => number;
  int: (min: number, max: number) => number;
  pick: <T>(items: T[]) => T;
  bool: (chance?: number) => boolean;
};

function hashSeed(seed: string): number {
  let hash = 0;
  for (let i = 0; i < seed.length; i += 1) {
    hash = (hash << 5) - hash + seed.charCodeAt(i);
    hash |= 0;
  }
  return hash >>> 0;
}

export function createSeededRng(seed: string): ForumRng {
  let t = hashSeed(seed) || 1;
  const next = () => {
    t += 0x6d2b79f5;
    let r = t;
    r = Math.imul(r ^ (r >>> 15), r | 1);
    r ^= r + Math.imul(r ^ (r >>> 7), r | 61);
    return ((r ^ (r >>> 14)) >>> 0) / 4294967296;
  };

  return {
    next,
    int: (min, max) => {
      if (max <= min) return min;
      return Math.floor(next() * (max - min + 1)) + min;
    },
    pick: (items) => {
      if (!items.length) {
        throw new Error("Cannot pick from empty array");
      }
      return items[Math.floor(next() * items.length)];
    },
    bool: (chance = 0.5) => next() < chance,
  };
}

function createLayout(rng: ForumRng): ForumMapLayout {
  const cols = rng.int(24, 32);
  const rows = rng.int(18, 26);
  return {
    id: "layout-main",
    name: "Forum Field",
    grid: { cols, rows },
    tile: { width: 64, height: 32 },
    origin: { x: cols * 18, y: 40 },
  };
}

function createBoundsForRoom(index: number, total: number, layout: ForumMapLayout, rng: ForumRng): ForumBounds {
  const columns = Math.ceil(Math.sqrt(total));
  const rows = Math.ceil(total / columns);
  const colIndex = index % columns;
  const rowIndex = Math.floor(index / columns);
  const baseWidth = Math.max(6, Math.floor(layout.grid.cols / columns));
  const baseHeight = Math.max(6, Math.floor(layout.grid.rows / rows));
  const inset = rng.int(1, 2);
  const minCol = colIndex * baseWidth + inset;
  const minRow = rowIndex * baseHeight + inset;
  const maxCol = Math.min(layout.grid.cols - 1, (colIndex + 1) * baseWidth - inset);
  const maxRow = Math.min(layout.grid.rows - 1, (rowIndex + 1) * baseHeight - inset);
  return {
    minCol,
    minRow,
    maxCol: Math.max(minCol + 2, maxCol),
    maxRow: Math.max(minRow + 2, maxRow),
  };
}

function randomPointInBounds(rng: ForumRng, bounds: ForumBounds): ForumGridPoint {
  return {
    col: rng.int(bounds.minCol, bounds.maxCol),
    row: rng.int(bounds.minRow, bounds.maxRow),
  };
}

function createWorkspaces(config: Required<Pick<ForumReplayConfig, "workspaceCount" | "repoGroupsPerWorkspace">>): {
  workspaces: ForumWorkspace[];
  repoGroups: ForumRepoGroup[];
} {
  const workspaces: ForumWorkspace[] = [];
  const repoGroups: ForumRepoGroup[] = [];

  for (let w = 0; w < config.workspaceCount; w += 1) {
    const workspaceId = `ws-${w + 1}`;
    const groups: string[] = [];
    for (let g = 0; g < config.repoGroupsPerWorkspace; g += 1) {
      const groupId = `${workspaceId}-repo-${g + 1}`;
      groups.push(groupId);
      repoGroups.push({
        id: groupId,
        name: `Repo Group ${g + 1}`,
        workspaceId,
        roomIds: [],
      });
    }
    workspaces.push({
      id: workspaceId,
      name: `Workspace ${w + 1}`,
      repoGroups: groups,
    });
  }

  return { workspaces, repoGroups };
}

function createRooms(
  roomCount: number,
  layout: ForumMapLayout,
  workspaces: ForumWorkspace[],
  repoGroups: ForumRepoGroup[],
  rng: ForumRng,
): ForumRoom[] {
  const rooms: ForumRoom[] = [];
  for (let i = 0; i < roomCount; i += 1) {
    const bounds = createBoundsForRoom(i, roomCount, layout, rng);
    const center = {
      col: Math.round((bounds.minCol + bounds.maxCol) / 2),
      row: Math.round((bounds.minRow + bounds.maxRow) / 2),
    };
    const workspace = workspaces[i % workspaces.length];
    const repoGroup = repoGroups[i % repoGroups.length];
    const room: ForumRoom = {
      id: `room-${i + 1}`,
      name: `Room ${i + 1}`,
      workspaceId: workspace.id,
      repoGroupId: repoGroup.id,
      bounds,
      center,
    };
    rooms.push(room);
    if (!repoGroup.roomIds.includes(room.id)) {
      repoGroup.roomIds.push(room.id);
    }
  }
  return rooms;
}

function createInitialEntities(
  rooms: ForumRoom[],
  unitsPerRoom: number,
  workersPerRoom: number,
  rng: ForumRng,
): {
  entities: ForumEntity[];
  workers: ForumWorker[];
} {
  const entities: ForumEntity[] = [];
  const workers: ForumWorker[] = [];

  rooms.forEach((room, roomIndex) => {
    for (let i = 0; i < unitsPerRoom; i += 1) {
      entities.push({
        id: `unit-${roomIndex + 1}-${i + 1}`,
        type: "unit",
        roomId: room.id,
        position: randomPointInBounds(rng, room.bounds),
        status: "idle",
        label: `Unit ${roomIndex + 1}.${i + 1}`,
      });
    }

    for (let i = 0; i < 2; i += 1) {
      entities.push({
        id: `structure-${roomIndex + 1}-${i + 1}`,
        type: "structure",
        roomId: room.id,
        position: randomPointInBounds(rng, room.bounds),
        status: "idle",
        label: `Depot ${roomIndex + 1}.${i + 1}`,
      });
    }

    for (let i = 0; i < workersPerRoom; i += 1) {
      const entityId = `worker-entity-${roomIndex + 1}-${i + 1}`;
      entities.push({
        id: entityId,
        type: "worker",
        roomId: room.id,
        position: randomPointInBounds(rng, room.bounds),
        status: "idle",
        label: `Worker ${roomIndex + 1}.${i + 1}`,
      });
      workers.push({
        id: `worker-${roomIndex + 1}-${i + 1}`,
        name: `Worker ${roomIndex + 1}.${i + 1}`,
        status: "idle",
        roomId: room.id,
        entityId,
      });
    }
  });

  return { entities, workers };
}

function createInitialTasks(
  rooms: ForumRoom[],
  tasksPerRoom: number,
  workers: ForumWorker[],
  rng: ForumRng,
  startTime: number,
): ForumTask[] {
  const tasks: ForumTask[] = [];
  rooms.forEach((room, roomIndex) => {
    for (let i = 0; i < tasksPerRoom; i += 1) {
      const worker = workers.length ? workers[(roomIndex * tasksPerRoom + i) % workers.length] : undefined;
      const task: ForumTask = {
        id: `task-${roomIndex + 1}-${i + 1}`,
        title: `Task ${roomIndex + 1}.${i + 1}`,
        status: rng.bool(0.4) ? "running" : "queued",
        roomId: room.id,
        workerId: worker?.id,
        entityId: worker?.entityId,
        progress: rng.bool(0.3) ? rng.next() * 0.4 : 0,
        createdAt: startTime,
        updatedAt: startTime,
      };
      tasks.push(task);
    }
  });
  return tasks;
}

export function generateForumReplay(config: ForumReplayConfig): ForumReplayScenario {
  const rng = createSeededRng(config.seed);
  const durationMs = config.durationMs ?? 60_000;
  const tickMs = config.tickMs ?? 1_000;
  const roomCount = config.roomCount ?? 4;
  const unitsPerRoom = config.unitsPerRoom ?? 6;
  const tasksPerRoom = config.tasksPerRoom ?? 4;
  const workersPerRoom = config.workersPerRoom ?? 2;
  const workspaceCount = config.workspaceCount ?? 1;
  const repoGroupsPerWorkspace = config.repoGroupsPerWorkspace ?? 2;

  const layout = createLayout(rng);
  const { workspaces, repoGroups } = createWorkspaces({ workspaceCount, repoGroupsPerWorkspace });
  const rooms = createRooms(roomCount, layout, workspaces, repoGroups, rng);
  const startTime = 0;

  const { entities, workers } = createInitialEntities(rooms, unitsPerRoom, workersPerRoom, rng);
  const tasks = createInitialTasks(rooms, tasksPerRoom, workers, rng, startTime);

  const events: ForumReplayEvent[] = [];
  let seq = 0;

  const pushEvent = (event: ForumReplayEventInput) => {
    events.push({
      ...event,
      id: `evt-${seq}`,
      seq,
    } as ForumReplayEvent);
    seq += 1;
  };

  pushEvent({ t: startTime, type: "layout.set", layout });
  rooms.forEach((room) => pushEvent({ t: startTime, type: "room.add", room }));
  entities.forEach((entity) => pushEvent({ t: startTime, type: "entity.add", entity }));
  workers.forEach((worker) => pushEvent({ t: startTime, type: "worker.add", worker }));
  tasks.forEach((task) => pushEvent({ t: startTime, type: "task.add", task }));

  const taskProgress = new Map(tasks.map((task) => [task.id, task.progress]));
  const taskStatus = new Map(tasks.map((task) => [task.id, task.status]));
  const activeEntityIds = entities.filter((entity) => entity.type === "unit").map((entity) => entity.id);

  for (let t = tickMs; t <= durationMs; t += tickMs) {
    if (activeEntityIds.length && rng.bool(0.6)) {
      const entityId = rng.pick(activeEntityIds);
      const entity = entities.find((item) => item.id === entityId);
      if (entity) {
        const room = rooms.find((item) => item.id === entity.roomId) ?? rooms[0];
        const position = randomPointInBounds(rng, room.bounds);
        pushEvent({
          t,
          type: "entity.move",
          entityId,
          roomId: room.id,
          position,
          status: rng.bool(0.3) ? "moving" : "idle",
        });
      }
    }

    if (tasks.length && rng.bool(0.7)) {
      const task = rng.pick(tasks);
      const currentStatus = taskStatus.get(task.id) ?? "queued";
      const currentProgress = taskProgress.get(task.id) ?? 0;
      if (currentStatus === "queued" && rng.bool(0.5)) {
        taskStatus.set(task.id, "running");
        pushEvent({
          t,
          type: "task.update",
          taskId: task.id,
          status: "running",
          updatedAt: t,
        });
      } else if (currentStatus === "running") {
        const delta = rng.next() * 0.35 + 0.1;
        const nextProgress = Math.min(1, currentProgress + delta);
        taskProgress.set(task.id, nextProgress);
        if (nextProgress >= 1) {
          const success = rng.bool(0.85);
          taskStatus.set(task.id, success ? "success" : "failed");
          pushEvent({
            t,
            type: "task.resolve",
            taskId: task.id,
            status: success ? "success" : "failed",
            progress: nextProgress,
            updatedAt: t,
          });
          if (!success) {
            const alert: ForumAlert = {
              id: `alert-${task.id}-${t}`,
              level: "L2",
              message: `Task ${task.id} failed in ${task.roomId}`,
              roomId: task.roomId,
              taskId: task.id,
              createdAt: t,
            };
            pushEvent({ t, type: "alert.raise", alert });
          }
        } else {
          pushEvent({
            t,
            type: "task.update",
            taskId: task.id,
            progress: nextProgress,
            updatedAt: t,
          });
        }
      }
    }

    if (tasks.length && rng.bool(0.2)) {
      const task = rng.pick(tasks);
      const alert: ForumAlert = {
        id: `alert-${task.id}-${t}-ping`,
        level: rng.bool(0.3) ? "L3" : "L1",
        message: `Attention on ${task.title}`,
        roomId: task.roomId,
        taskId: task.id,
        createdAt: t,
      };
      pushEvent({ t, type: "alert.raise", alert });
    }

    if (rng.bool(0.1)) {
      const room = rng.pick(rooms);
      const marker: ForumMarker = {
        id: `marker-${room.id}-${t}`,
        type: "ping",
        roomId: room.id,
        position: randomPointInBounds(rng, room.bounds),
        label: "Ping",
        createdAt: t,
        expiresAt: t + tickMs * 2,
      };
      pushEvent({ t, type: "marker.add", marker });
    }
  }

  return {
    seed: config.seed,
    createdAt: 0,
    durationMs,
    tickMs,
    layout,
    workspaces,
    repoGroups,
    rooms,
    events,
  };
}

export function hydrateForumReplay(scenario: ForumReplayScenario, untilMs = scenario.durationMs): ForumMapState {
  const state = createForumState({
    layout: scenario.layout,
    workspaces: scenario.workspaces,
    repoGroups: scenario.repoGroups,
    rooms: scenario.rooms,
  });
  const events = scenario.events
    .filter((event) => event.t <= untilMs)
    .sort((a, b) => (a.t === b.t ? a.seq - b.seq : a.t - b.t));
  return applyForumEvents(state, events);
}

export function getReplayEventsUpTo(scenario: ForumReplayScenario, timeMs: number): ForumReplayEvent[] {
  return scenario.events
    .filter((event) => event.t <= timeMs)
    .sort((a, b) => (a.t === b.t ? a.seq - b.seq : a.t - b.t));
}

export type ForumReplayCursor = {
  scenario: ForumReplayScenario;
  state: ForumMapState;
  events: ForumReplayEvent[];
  index: number;
  now: number;
};

export function createForumReplayCursor(scenario: ForumReplayScenario): ForumReplayCursor {
  const events = [...scenario.events].sort((a, b) => (a.t === b.t ? a.seq - b.seq : a.t - b.t));
  const state = createForumState({
    layout: scenario.layout,
    workspaces: scenario.workspaces,
    repoGroups: scenario.repoGroups,
    rooms: scenario.rooms,
  });
  let index = 0;
  while (index < events.length && events[index].t <= 0) {
    applyForumEvents(state, [events[index]]);
    index += 1;
  }
  return { scenario, state, events, index, now: 0 };
}

export function advanceForumReplay(cursor: ForumReplayCursor, targetTime: number): number {
  let applied = 0;
  while (cursor.index < cursor.events.length && cursor.events[cursor.index].t <= targetTime) {
    applyForumEvents(cursor.state, [cursor.events[cursor.index]]);
    cursor.index += 1;
    applied += 1;
  }
  cursor.now = Math.max(cursor.now, targetTime);
  // Also update state.now so marker expiry works even on eventless frames
  cursor.state.now = Math.max(cursor.state.now, targetTime);
  return applied;
}
