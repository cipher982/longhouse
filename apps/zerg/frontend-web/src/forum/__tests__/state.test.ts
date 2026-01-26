import { describe, expect, it } from "vitest";
import { generateForumReplay } from "../replay";
import { applyForumEvents, createForumState } from "../state";
import type { ForumAlert, ForumMarker } from "../types";

const scenario = generateForumReplay({
  seed: "state-seed",
  durationMs: 2000,
  tickMs: 1000,
  roomCount: 2,
  unitsPerRoom: 1,
  tasksPerRoom: 1,
  workersPerRoom: 1,
});

describe("forum state", () => {
  it("applies events idempotently", () => {
    const state = createForumState({
      layout: scenario.layout,
      workspaces: scenario.workspaces,
      repoGroups: scenario.repoGroups,
      rooms: scenario.rooms,
    });

    applyForumEvents(state, scenario.events);
    const entitiesAfterFirst = state.entities.size;
    const tasksAfterFirst = state.tasks.size;
    const alertsAfterFirst = state.alerts.size;

    applyForumEvents(state, scenario.events);
    expect(state.entities.size).toBe(entitiesAfterFirst);
    expect(state.tasks.size).toBe(tasksAfterFirst);
    expect(state.alerts.size).toBe(alertsAfterFirst);
  });

  it("adds and clears alerts and markers", () => {
    const state = createForumState({
      layout: scenario.layout,
      workspaces: scenario.workspaces,
      repoGroups: scenario.repoGroups,
      rooms: scenario.rooms,
    });

    const room = scenario.rooms[0];
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
