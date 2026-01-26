import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { Badge, Button, Card, PageShell, SectionHeader } from "../components/ui";
import { generateSwarmReplay } from "../swarm/replay";
import { SwarmMapCanvas } from "../swarm/SwarmMapCanvas";
import { useSwarmReplayPlayer } from "../swarm/useSwarmReplay";
import { eventBus } from "../jarvis/lib/event-bus";
import type {
  SwarmAlert,
  SwarmEntity,
  SwarmMarker,
  SwarmReplayEvent,
  SwarmReplayEventInput,
  SwarmTask,
} from "../swarm/types";
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
    dispatchEvent,
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

  const getDefaultRoom = () => {
    const iterator = stateRef.current.rooms.values().next();
    return iterator.done ? null : iterator.value;
  };

  const positionForId = (id: string, bounds: { minCol: number; minRow: number; maxCol: number; maxRow: number }) => {
    const hash = Array.from(id).reduce((acc, char) => acc + char.charCodeAt(0), 0);
    const spanCol = Math.max(1, bounds.maxCol - bounds.minCol);
    const spanRow = Math.max(1, bounds.maxRow - bounds.minRow);
    return {
      col: bounds.minCol + (hash % spanCol),
      row: bounds.minRow + ((hash * 7) % spanRow),
    };
  };

  const nudgeTask = (task: SwarmTask) => {
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
    const ensureRoom = () => getDefaultRoom();

    const ensureTaskEntity = (taskId: string, roomId: string, timestamp: number) => {
      const entityId = `task-node-${taskId}`;
      if (stateRef.current.entities.has(entityId)) return entityId;
      const room = stateRef.current.rooms.get(roomId);
      if (!room) return entityId;
      const entity: SwarmEntity = {
        id: entityId,
        type: "task_node",
        roomId,
        position: positionForId(entityId, room.bounds),
        status: "working",
        label: `Task Node ${taskId}`,
      };
      dispatchEvent(makeLocalEvent({ t: timestamp, type: "entity.add", entity }));
      return entityId;
    };

    unsubscribers.push(
      eventBus.on("supervisor:started", (data) => {
        const room = ensureRoom();
        if (!room) return;
        const taskId = `run-${data.runId}`;
        const entityId = ensureTaskEntity(taskId, room.id, data.timestamp);
        if (!stateRef.current.tasks.has(taskId)) {
          const task: SwarmTask = {
            id: taskId,
            title: data.task,
            status: "running",
            roomId: room.id,
            entityId,
            progress: 0,
            createdAt: data.timestamp,
            updatedAt: data.timestamp,
          };
          dispatchEvent(makeLocalEvent({ t: data.timestamp, type: "task.add", task }));
        }
      }),
    );

    unsubscribers.push(
      eventBus.on("supervisor:worker_spawned", (data) => {
        const room = ensureRoom();
        if (!room) return;
        const workerId = `worker-${data.jobId}`;
        const entityId = `worker-entity-${data.jobId}`;
        if (!stateRef.current.entities.has(entityId)) {
          const entity: SwarmEntity = {
            id: entityId,
            type: "worker",
            roomId: room.id,
            position: positionForId(entityId, room.bounds),
            status: "working",
            label: `Worker ${data.jobId}`,
          };
          dispatchEvent(makeLocalEvent({ t: data.timestamp, type: "entity.add", entity }));
        }
        if (!stateRef.current.workers.has(workerId)) {
          dispatchEvent(
            makeLocalEvent({
              t: data.timestamp,
              type: "worker.add",
              worker: {
                id: workerId,
                name: `Worker ${data.jobId}`,
                status: "busy",
                roomId: room.id,
                entityId,
              },
            }),
          );
        }
        const taskId = `job-${data.jobId}`;
        if (!stateRef.current.tasks.has(taskId)) {
          const task: SwarmTask = {
            id: taskId,
            title: data.task,
            status: "running",
            roomId: room.id,
            entityId,
            workerId,
            progress: 0,
            createdAt: data.timestamp,
            updatedAt: data.timestamp,
          };
          dispatchEvent(makeLocalEvent({ t: data.timestamp, type: "task.add", task }));
        }
      }),
    );

    unsubscribers.push(
      eventBus.on("supervisor:worker_complete", (data) => {
        const workerId = `worker-${data.jobId}`;
        if (stateRef.current.workers.has(workerId)) {
          dispatchEvent(
            makeLocalEvent({
              t: data.timestamp,
              type: "worker.update",
              workerId,
              status: data.status === "success" ? "idle" : "offline",
            }),
          );
        }
        const taskId = `job-${data.jobId}`;
        if (stateRef.current.tasks.has(taskId)) {
          dispatchEvent(
            makeLocalEvent({
              t: data.timestamp,
              type: "task.resolve",
              taskId,
              status: data.status === "success" ? "success" : "failed",
              progress: 1,
              updatedAt: data.timestamp,
            }),
          );
        }
        if (data.status !== "success") {
          const room = ensureRoom();
          if (!room) return;
          const alert: SwarmAlert = {
            id: `alert-worker-${data.jobId}-${data.timestamp}`,
            level: "L2",
            message: `Worker ${data.jobId} failed`,
            roomId: room.id,
            createdAt: data.timestamp,
          };
          dispatchEvent(makeLocalEvent({ t: data.timestamp, type: "alert.raise", alert }));
        }
      }),
    );

    unsubscribers.push(
      eventBus.on("supervisor:complete", (data) => {
        const taskId = `run-${data.runId}`;
        if (!stateRef.current.tasks.has(taskId)) return;
        dispatchEvent(
          makeLocalEvent({
            t: data.timestamp,
            type: "task.resolve",
            taskId,
            status: data.status === "success" ? "success" : "failed",
            progress: 1,
            updatedAt: data.timestamp,
          }),
        );
      }),
    );

    unsubscribers.push(
      eventBus.on("worker:tool_failed", (data) => {
        const room = ensureRoom();
        if (!room) return;
        const alert: SwarmAlert = {
          id: `alert-tool-${data.toolCallId}-${data.timestamp}`,
          level: "L1",
          message: `${data.toolName} failed`,
          roomId: room.id,
          createdAt: data.timestamp,
        };
        dispatchEvent(makeLocalEvent({ t: data.timestamp, type: "alert.raise", alert }));
      }),
    );

    return () => {
      unsubscribers.forEach((unsubscribe) => unsubscribe());
    };
  }, [dispatchEvent, isLive, makeLocalEvent]);

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
        </Card>
      </div>
    </PageShell>
  );
}
