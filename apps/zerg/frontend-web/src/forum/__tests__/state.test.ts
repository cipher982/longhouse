import { describe, expect, it } from "vitest";
import { applyForumEvents, createForumState } from "../state";
import type { ForumAlert, ForumMarker } from "../types";

const layout = {
  id: "layout-test",
  name: "Forum",
  grid: { cols: 12, rows: 12 },
  tile: { width: 64, height: 32 },
  origin: { x: 0, y: 0 },
};

const room = {
  id: "room-1",
  name: "Room 1",
  workspaceId: "ws-1",
  repoGroupId: "rg-1",
  bounds: { minCol: 0, minRow: 0, maxCol: 5, maxRow: 5 },
  center: { col: 2, row: 2 },
};

describe("forum state", () => {
  it("applies events idempotently", () => {
    const state = createForumState({
      layout,
      rooms: [room],
    });

    applyForumEvents(state, [
      {
        id: "evt-entity-add",
        seq: 0,
        t: 0,
        type: "entity.add",
        entity: {
          id: "entity-1",
          type: "commis",
          roomId: room.id,
          position: room.center,
          status: "idle",
        },
      },
      {
        id: "evt-task-add",
        seq: 1,
        t: 1,
        type: "task.add",
        task: {
          id: "task-1",
          title: "Test task",
          status: "queued",
          roomId: room.id,
          progress: 0,
          createdAt: 0,
          updatedAt: 0,
        },
      },
    ]);
    const entitiesAfterFirst = state.entities.size;
    const tasksAfterFirst = state.tasks.size;
    const alertsAfterFirst = state.alerts.size;

    applyForumEvents(state, [
      {
        id: "evt-entity-add",
        seq: 0,
        t: 0,
        type: "entity.add",
        entity: {
          id: "entity-1",
          type: "commis",
          roomId: room.id,
          position: room.center,
          status: "idle",
        },
      },
      {
        id: "evt-task-add",
        seq: 1,
        t: 1,
        type: "task.add",
        task: {
          id: "task-1",
          title: "Test task",
          status: "queued",
          roomId: room.id,
          progress: 0,
          createdAt: 0,
          updatedAt: 0,
        },
      },
    ]);
    expect(state.entities.size).toBe(entitiesAfterFirst);
    expect(state.tasks.size).toBe(tasksAfterFirst);
    expect(state.alerts.size).toBe(alertsAfterFirst);
  });

  it("adds and clears alerts and markers", () => {
    const state = createForumState({
      layout,
      rooms: [room],
    });

    const alert: ForumAlert = {
      id: "alert-1",
      level: "L1",
      message: "Test alert",
      roomId: room.id,
      createdAt: 0,
    };
    const marker: ForumMarker = {
      id: "marker-1",
      type: "ping",
      roomId: room.id,
      position: room.center,
      createdAt: 0,
    };

    applyForumEvents(state, [
      { id: "evt-alert-add", seq: 0, t: 0, type: "alert.raise", alert },
      { id: "evt-marker-add", seq: 1, t: 0, type: "marker.add", marker },
    ]);

    expect(state.alerts.size).toBe(1);
    expect(state.markers.size).toBe(1);

    applyForumEvents(state, [
      { id: "evt-alert-clear", seq: 2, t: 1, type: "alert.clear", alertId: alert.id },
      { id: "evt-marker-clear", seq: 3, t: 1, type: "marker.clear", markerId: marker.id },
    ]);

    expect(state.alerts.size).toBe(0);
    expect(state.markers.size).toBe(0);
  });
});
