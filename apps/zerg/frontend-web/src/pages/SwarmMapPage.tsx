import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { Badge, Button, Card, PageShell, SectionHeader } from "../components/ui";
import { generateSwarmReplay } from "../swarm/replay";
import { SwarmMapCanvas } from "../swarm/SwarmMapCanvas";
import { useSwarmReplayPlayer } from "../swarm/useSwarmReplay";
import { eventBus } from "../jarvis/lib/event-bus";
import type {
  SwarmMarker,
  SwarmReplayEvent,
  SwarmReplayEventInput,
  SwarmTask,
} from "../swarm/types";
import {
  mapSupervisorComplete,
  mapSupervisorStarted,
  mapWorkerComplete,
  mapWorkerSpawned,
  mapWorkerToolFailed,
} from "../swarm/live-mapper";
import "../styles/swarm-map.css";

const DEFAULT_SEED = "swarm-demo";

export default function SwarmMapPage() {
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
      generateSwarmReplay({
        seed,
        durationMs: 90_000,
        tickMs: 1_000,
        roomCount: 4,
        unitsPerRoom: 6,
        tasksPerRoom: 4,
        workersPerRoom: 2,
        workspaceCount: 1,
        repoGroupsPerWorkspace: 2,
      }),
    [seed],
  );

  const liveScenario = useMemo(
    () =>
      generateSwarmReplay({
        seed: "swarm-live",
        durationMs: 120_000,
        tickMs: 1_000,
        roomCount: 2,
        unitsPerRoom: 0,
        tasksPerRoom: 0,
        workersPerRoom: 0,
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
    setPlaying,
    reset,
    dispatchEvents,
  } = useSwarmReplayPlayer(scenario, {
    loop: true,
    speed: 1,
    playing: !isLive,
  });

  const tasks = useMemo(() => {
    const list = Array.from(state.tasks.values());
    return list.sort((a, b) => b.updatedAt - a.updatedAt);
  }, [state.tasks, timeMs]);

  const selectedEntity = selectedEntityId ? state.entities.get(selectedEntityId) : null;
  const selectedTask = selectedTaskId ? state.tasks.get(selectedTaskId) : null;

  const stateRef = useRef(state);
  useEffect(() => {
    stateRef.current = state;
  }, [state]);

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
    (event: SwarmReplayEventInput): SwarmReplayEvent => {
      const seq = localSeqRef.current;
      localSeqRef.current += 1;
      return {
        ...event,
        id: `local-${Date.now()}-${seq}`,
        seq,
      } as SwarmReplayEvent;
    },
    [],
  );

  const nudgeTask = (task: SwarmTask) => {
    if (task.status === "success" || task.status === "failed" || task.progress >= 1) {
      return;
    }
    const now = Math.max(timeMs, stateRef.current.now);
    const nextProgress = Math.min(1, task.progress + 0.2);
    const events: SwarmReplayEvent[] = [];

    const marker: SwarmMarker = {
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
      eventBus.on("supervisor:started", (data) => {
        const events = mapSupervisorStarted(stateRef.current, data).map(makeLocalEvent);
        if (events.length) {
          dispatchEvents(events);
        }
      }),
    );

    unsubscribers.push(
      eventBus.on("supervisor:worker_spawned", (data) => {
        const events = mapWorkerSpawned(stateRef.current, data).map(makeLocalEvent);
        if (events.length) {
          dispatchEvents(events);
        }
      }),
    );

    unsubscribers.push(
      eventBus.on("supervisor:worker_complete", (data) => {
        const events = mapWorkerComplete(stateRef.current, data).map(makeLocalEvent);
        if (events.length) {
          dispatchEvents(events);
        }
      }),
    );

    unsubscribers.push(
      eventBus.on("supervisor:complete", (data) => {
        const events = mapSupervisorComplete(stateRef.current, data).map(makeLocalEvent);
        if (events.length) {
          dispatchEvents(events);
        }
      }),
    );

    unsubscribers.push(
      eventBus.on("worker:tool_failed", (data) => {
        const events = mapWorkerToolFailed(stateRef.current, data).map(makeLocalEvent);
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
    <PageShell size="full" className="swarm-map-page">
      <SectionHeader
        title="Swarm Map"
        description="Decision-driven overlay for the Swarm runtime"
        actions={
          <div className="swarm-map-actions">
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
              Swarm Ops
            </Button>
          </div>
        }
      />

      <div className="swarm-map-grid">
        <Card className="swarm-map-panel swarm-map-panel--left">
          <div className="swarm-panel-header">
            <div>
              <div className="swarm-panel-title">Command List</div>
              <div className="swarm-panel-subtitle">{tasks.length} tasks in motion</div>
            </div>
            <Badge variant={isLive ? "success" : "neutral"}>{isLive ? "Live" : `${Math.round((timeMs / durationMs) * 100)}%`}</Badge>
          </div>
          <div className="swarm-task-list">
            {tasks.length === 0 ? (
              <div className="swarm-task-empty">No tasks yet. Awaiting live signals or replay ticks.</div>
            ) : (
              tasks.map((task) => (
                <button
                  key={task.id}
                  className={`swarm-task-row${task.id === selectedTaskId ? " swarm-task-row--selected" : ""}`}
                  type="button"
                  onClick={() => {
                    setSelectedTaskId(task.id);
                    if (task.entityId) {
                      setSelectedEntityId(task.entityId);
                    }
                  }}
                >
                  <span className="swarm-task-title">{task.title}</span>
                  <span className="swarm-task-progress">{Math.round(task.progress * 100)}%</span>
                  <span className={`swarm-task-status swarm-task-status--${task.status}`}>{task.status}</span>
                </button>
              ))
            )}
          </div>
        </Card>

        <Card className="swarm-map-panel swarm-map-panel--center">
          <SwarmMapCanvas
            state={state}
            timeMs={timeMs}
            selectedEntityId={selectedEntityId}
            focusEntityId={focusEntityId}
            onSelectEntity={setSelectedEntityId}
          />
        </Card>

        <Card className="swarm-map-panel swarm-map-panel--right">
          <div className="swarm-panel-header">
            <div>
              <div className="swarm-panel-title">Drop-In</div>
              <div className="swarm-panel-subtitle">Selection details</div>
            </div>
            {selectedEntity || selectedTask ? <Badge variant="success">Active</Badge> : <Badge variant="neutral">Idle</Badge>}
          </div>
          <div className="swarm-selection">
            {selectedEntity ? (
              <>
                <div className="swarm-selection-title">{selectedEntity.label ?? selectedEntity.id}</div>
                <div className="swarm-selection-meta">Type: {selectedEntity.type}</div>
                <div className="swarm-selection-meta">Room: {selectedEntity.roomId}</div>
                <div className="swarm-selection-meta">Status: {selectedEntity.status}</div>
                <div className="swarm-selection-actions">
                  <Button size="sm" variant="primary" onClick={handleFocus}>
                    {focusEntityId === selectedEntity.id ? "Unfocus" : "Focus"}
                  </Button>
                </div>
              </>
            ) : null}

            {selectedTask ? (
              <div className="swarm-selection-task">
                <div className="swarm-selection-title">Task: {selectedTask.title}</div>
                <div className="swarm-selection-meta">Status: {selectedTask.status}</div>
                <div className="swarm-selection-meta">Progress: {Math.round(selectedTask.progress * 100)}%</div>
                <div className="swarm-selection-actions">
                  <Button size="sm" variant="ghost" onClick={() => nudgeTask(selectedTask)}>
                    Nudge Task
                  </Button>
                </div>
              </div>
            ) : null}

            {!selectedEntity && !selectedTask ? (
              <div className="swarm-selection-empty">Select a unit or task to inspect.</div>
            ) : null}
          </div>
          <div className="swarm-legend">
            <div className="swarm-legend-title">Legend</div>
            <div className="swarm-legend-grid">
              <div className="swarm-legend-item">
                <span className="swarm-legend-swatch swarm-legend-swatch--unit" />
                Unit
              </div>
              <div className="swarm-legend-item">
                <span className="swarm-legend-swatch swarm-legend-swatch--structure" />
                Structure
              </div>
              <div className="swarm-legend-item">
                <span className="swarm-legend-swatch swarm-legend-swatch--worker" />
                Worker
              </div>
              <div className="swarm-legend-item">
                <span className="swarm-legend-swatch swarm-legend-swatch--task" />
                Task Node
              </div>
              <div className="swarm-legend-item">
                <span className="swarm-legend-swatch swarm-legend-swatch--alert" />
                Alert Ring
              </div>
              <div className="swarm-legend-item">
                <span className="swarm-legend-swatch swarm-legend-swatch--marker" />
                Marker Ping
              </div>
            </div>
          </div>
        </Card>
      </div>
    </PageShell>
  );
}
