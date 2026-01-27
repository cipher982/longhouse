import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { Badge, Button, Card, PageShell, SectionHeader } from "../components/ui";
import { generateForumReplay } from "../forum/replay";
import { ForumCanvas } from "../forum/ForumCanvas";
import { useForumReplayPlayer } from "../forum/useForumReplay";
import { eventBus } from "../oikos/lib/event-bus";
import type {
  ForumMarker,
  ForumReplayEvent,
  ForumReplayEventInput,
  ForumTask,
} from "../forum/types";
import {
  mapOikosComplete,
  mapOikosStarted,
  mapCommisComplete,
  mapCommisSpawned,
  mapCommisToolFailed,
} from "../forum/live-mapper";
import "../styles/forum.css";

const DEFAULT_SEED = "forum-demo";

export default function ForumPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const params = new URLSearchParams(location.search);
  const seed = params.get("seed")?.trim() || DEFAULT_SEED;

  const [selectedEntityId, setSelectedEntityId] = useState<string | null>(null);
  const [focusEntityId, setFocusEntityId] = useState<string | null>(null);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [mode, setMode] = useState<"replay" | "live">("replay");
  const isLive = mode === "live";

  const replayScenario = useMemo(
    () =>
      generateForumReplay({
        seed,
        durationMs: 90_000,
        tickMs: 1_000,
        roomCount: 4,
        unitsPerRoom: 6,
        tasksPerRoom: 4,
        commissPerRoom: 2,
        workspaceCount: 1,
        repoGroupsPerWorkspace: 2,
      }),
    [seed],
  );

  const liveScenario = useMemo(
    () =>
      generateForumReplay({
        seed: "forum-live",
        durationMs: 120_000,
        tickMs: 1_000,
        roomCount: 2,
        unitsPerRoom: 0,
        tasksPerRoom: 0,
        commissPerRoom: 0,
        workspaceCount: 1,
        repoGroupsPerWorkspace: 1,
      }),
    [],
  );

  const scenario = isLive ? liveScenario : replayScenario;

  const {
    state,
    timeMs,
    durationMs,
    playing,
    stateVersion,
    setPlaying,
    reset,
    dispatchEvents,
  } = useForumReplayPlayer(scenario, {
    loop: true,
    speed: 1,
    playing: !isLive,
  });

  // Use stateVersion instead of state.tasks directly since Map reference doesn't change on mutation
  const tasks = useMemo(() => {
    const list = Array.from(state.tasks.values());
    return list.sort((a, b) => b.updatedAt - a.updatedAt);
  }, [state.tasks, stateVersion]);

  const selectedEntity = selectedEntityId ? state.entities.get(selectedEntityId) : null;
  const selectedTask = selectedTaskId ? state.tasks.get(selectedTaskId) : null;

  // Use ref for synchronous access in event handlers
  // Updated synchronously after dispatchEvents to handle fast back-to-back events
  const stateRef = useRef(state);
  stateRef.current = state;

  useEffect(() => {
    if (selectedEntityId && !state.entities.has(selectedEntityId)) {
      setSelectedEntityId(null);
    }
    if (selectedTaskId && !state.tasks.has(selectedTaskId)) {
      setSelectedTaskId(null);
    }
    if (focusEntityId && !state.entities.has(focusEntityId)) {
      setFocusEntityId(null);
    }
  }, [focusEntityId, selectedEntityId, selectedTaskId, state.entities, state.tasks, timeMs]);

  const handleFocus = () => {
    if (!selectedEntityId) return;
    setFocusEntityId((prev) => (prev === selectedEntityId ? null : selectedEntityId));
  };

  const localSeqRef = useRef(0);
  const makeLocalEvent = useCallback(
    (event: ForumReplayEventInput): ForumReplayEvent => {
      const seq = localSeqRef.current;
      localSeqRef.current += 1;
      return {
        ...event,
        id: `local-${Date.now()}-${seq}`,
        seq,
      } as ForumReplayEvent;
    },
    [],
  );

  const nudgeTask = (task: ForumTask) => {
    if (task.status === "success" || task.status === "failed" || task.progress >= 1) {
      return;
    }
    const now = Math.max(timeMs, stateRef.current.now);
    const nextProgress = Math.min(1, task.progress + 0.2);
    const events: ForumReplayEvent[] = [];

    const marker: ForumMarker = {
      id: `marker-${task.id}-${now}`,
      type: "focus",
      roomId: task.roomId,
      position:
        task.entityId && stateRef.current.entities.has(task.entityId)
          ? stateRef.current.entities.get(task.entityId)!.position
          : stateRef.current.rooms.get(task.roomId)?.center ?? { col: 0, row: 0 },
      label: "Nudge",
      createdAt: now,
      expiresAt: now + 2000,
    };

    events.push(makeLocalEvent({ t: now, type: "marker.add", marker }));

    if (nextProgress >= 1) {
      events.push(
        makeLocalEvent({
          t: now,
          type: "task.resolve",
          taskId: task.id,
          status: "success",
          progress: 1,
          updatedAt: now,
        }),
      );
    } else {
      events.push(
        makeLocalEvent({
          t: now,
          type: "task.update",
          taskId: task.id,
          status: "running",
          progress: nextProgress,
          updatedAt: now,
        }),
      );
    }

    dispatchEvents(events);
  };

  useEffect(() => {
    setPlaying(!isLive);
    reset();
  }, [isLive, reset, setPlaying]);

  useEffect(() => {
    if (!isLive) return;

    const unsubscribers: Array<() => void> = [];

    unsubscribers.push(
      eventBus.on("oikos:started", (data) => {
        const events = mapOikosStarted(stateRef.current, data).map(makeLocalEvent);
        if (events.length) {
          dispatchEvents(events);
        }
      }),
    );

    unsubscribers.push(
      eventBus.on("oikos:commis_spawned", (data) => {
        const events = mapCommisSpawned(stateRef.current, data).map(makeLocalEvent);
        if (events.length) {
          dispatchEvents(events);
        }
      }),
    );

    unsubscribers.push(
      eventBus.on("oikos:commis_complete", (data) => {
        const events = mapCommisComplete(stateRef.current, data).map(makeLocalEvent);
        if (events.length) {
          dispatchEvents(events);
        }
      }),
    );

    unsubscribers.push(
      eventBus.on("oikos:complete", (data) => {
        const events = mapOikosComplete(stateRef.current, data).map(makeLocalEvent);
        if (events.length) {
          dispatchEvents(events);
        }
      }),
    );

    unsubscribers.push(
      eventBus.on("commis:tool_failed", (data) => {
        const events = mapCommisToolFailed(stateRef.current, data).map(makeLocalEvent);
        if (events.length) {
          dispatchEvents(events);
        }
      }),
    );

    return () => {
      unsubscribers.forEach((unsubscribe) => unsubscribe());
    };
  }, [dispatchEvents, isLive, makeLocalEvent]);

  return (
    <PageShell size="full" className="forum-map-page">
      <SectionHeader
        title="The Forum"
        description="Decision-driven command overlay"
        actions={
          <div className="forum-map-actions">
            <Button variant="secondary" size="sm" onClick={() => setPlaying(!playing)} disabled={isLive}>
              {playing ? "Pause" : "Play"}
            </Button>
            <Button variant="secondary" size="sm" onClick={reset}>
              Reset
            </Button>
            <Button variant={isLive ? "primary" : "ghost"} size="sm" onClick={() => setMode(isLive ? "replay" : "live")}>
              {isLive ? "Live Signals" : "Replay Mode"}
            </Button>
            <Button variant="ghost" size="sm" onClick={() => navigate("/swarm/ops")}>
              Ops List
            </Button>
          </div>
        }
      />

      <div className="forum-map-grid">
        <Card className="forum-map-panel forum-map-panel--left">
          <div className="forum-panel-header">
            <div>
              <div className="forum-panel-title">Command List</div>
              <div className="forum-panel-subtitle">{tasks.length} tasks in motion</div>
            </div>
            <Badge variant={isLive ? "success" : "neutral"}>{isLive ? "Live" : `${Math.round((timeMs / durationMs) * 100)}%`}</Badge>
          </div>
          <div className="forum-task-list">
            {tasks.length === 0 ? (
              <div className="forum-task-empty">No tasks yet. Awaiting live signals or replay ticks.</div>
            ) : (
              tasks.map((task) => (
                <button
                  key={task.id}
                  className={`forum-task-row${task.id === selectedTaskId ? " forum-task-row--selected" : ""}`}
                  type="button"
                  onClick={() => {
                    setSelectedTaskId(task.id);
                    if (task.entityId) {
                      setSelectedEntityId(task.entityId);
                    }
                  }}
                >
                  <span className="forum-task-title">{task.title}</span>
                  <span className="forum-task-progress">{Math.round(task.progress * 100)}%</span>
                  <span className={`forum-task-status forum-task-status--${task.status}`}>{task.status}</span>
                </button>
              ))
            )}
          </div>
        </Card>

        <Card className="forum-map-panel forum-map-panel--center">
          <ForumCanvas
            state={state}
            timeMs={timeMs}
            selectedEntityId={selectedEntityId}
            focusEntityId={focusEntityId}
            onSelectEntity={setSelectedEntityId}
          />
        </Card>

        <Card className="forum-map-panel forum-map-panel--right">
          <div className="forum-panel-header">
            <div>
              <div className="forum-panel-title">Drop-In</div>
              <div className="forum-panel-subtitle">Selection details</div>
            </div>
            {selectedEntity || selectedTask ? <Badge variant="success">Active</Badge> : <Badge variant="neutral">Idle</Badge>}
          </div>
          <div className="forum-selection">
            {selectedEntity ? (
              <>
                <div className="forum-selection-title">{selectedEntity.label ?? selectedEntity.id}</div>
                <div className="forum-selection-meta">Type: {selectedEntity.type}</div>
                <div className="forum-selection-meta">Room: {selectedEntity.roomId}</div>
                <div className="forum-selection-meta">Status: {selectedEntity.status}</div>
                <div className="forum-selection-actions">
                  <Button size="sm" variant="primary" onClick={handleFocus}>
                    {focusEntityId === selectedEntity.id ? "Unfocus" : "Focus"}
                  </Button>
                </div>
              </>
            ) : null}

            {selectedTask ? (
              <div className="forum-selection-task">
                <div className="forum-selection-title">Task: {selectedTask.title}</div>
                <div className="forum-selection-meta">Status: {selectedTask.status}</div>
                <div className="forum-selection-meta">Progress: {Math.round(selectedTask.progress * 100)}%</div>
                <div className="forum-selection-actions">
                  <Button size="sm" variant="ghost" onClick={() => nudgeTask(selectedTask)}>
                    Nudge Task
                  </Button>
                </div>
              </div>
            ) : null}

            {!selectedEntity && !selectedTask ? (
              <div className="forum-selection-empty">Select a unit or task to inspect.</div>
            ) : null}
          </div>
          <div className="forum-legend">
            <div className="forum-legend-title">Legend</div>
            <div className="forum-legend-grid">
              <div className="forum-legend-item">
                <span className="forum-legend-swatch forum-legend-swatch--unit" />
                Unit
              </div>
              <div className="forum-legend-item">
                <span className="forum-legend-swatch forum-legend-swatch--structure" />
                Structure
              </div>
              <div className="forum-legend-item">
                <span className="forum-legend-swatch forum-legend-swatch--commis" />
                Commis
              </div>
              <div className="forum-legend-item">
                <span className="forum-legend-swatch forum-legend-swatch--task" />
                Task Node
              </div>
              <div className="forum-legend-item">
                <span className="forum-legend-swatch forum-legend-swatch--alert" />
                Alert Ring
              </div>
              <div className="forum-legend-item">
                <span className="forum-legend-swatch forum-legend-swatch--marker" />
                Marker Ping
              </div>
            </div>
          </div>
        </Card>
      </div>
    </PageShell>
  );
}
