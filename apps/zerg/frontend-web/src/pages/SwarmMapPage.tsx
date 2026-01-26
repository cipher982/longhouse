import { useEffect, useMemo, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { Badge, Button, Card, PageShell, SectionHeader } from "../components/ui";
import { generateSwarmReplay } from "../swarm/replay";
import { SwarmMapCanvas } from "../swarm/SwarmMapCanvas";
import { useSwarmReplayPlayer } from "../swarm/useSwarmReplay";
import "../styles/swarm-map.css";

const DEFAULT_SEED = "swarm-demo";

export default function SwarmMapPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const params = new URLSearchParams(location.search);
  const seed = params.get("seed")?.trim() || DEFAULT_SEED;
  const [selectedEntityId, setSelectedEntityId] = useState<string | null>(null);
  const [focusEntityId, setFocusEntityId] = useState<string | null>(null);

  const scenario = useMemo(
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

  const { state, timeMs, durationMs, playing, setPlaying, reset } = useSwarmReplayPlayer(scenario, {
    loop: true,
    speed: 1,
    playing: true,
  });

  const tasks = useMemo(() => {
    const list = Array.from(state.tasks.values());
    return list.sort((a, b) => b.updatedAt - a.updatedAt);
  }, [state.tasks, timeMs]);

  const selectedEntity = selectedEntityId ? state.entities.get(selectedEntityId) : null;

  useEffect(() => {
    if (selectedEntityId && !state.entities.has(selectedEntityId)) {
      setSelectedEntityId(null);
    }
    if (focusEntityId && !state.entities.has(focusEntityId)) {
      setFocusEntityId(null);
    }
  }, [focusEntityId, selectedEntityId, state.entities, timeMs]);

  const handleFocus = () => {
    if (!selectedEntityId) return;
    setFocusEntityId((prev) => (prev === selectedEntityId ? null : selectedEntityId));
  };

  return (
    <PageShell size="full" className="swarm-map-page">
      <SectionHeader
        title="Swarm Map"
        description="Decision-driven overlay for the Swarm runtime"
        actions={
          <div className="swarm-map-actions">
            <Button variant="secondary" size="sm" onClick={() => setPlaying(!playing)}>
              {playing ? "Pause" : "Play"}
            </Button>
            <Button variant="secondary" size="sm" onClick={reset}>
              Reset
            </Button>
            <Button variant="ghost" size="sm" onClick={() => navigate("/swarm/ops")}>Swarm Ops</Button>
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
            <Badge variant="neutral">{Math.round((timeMs / durationMs) * 100)}%</Badge>
          </div>
          <div className="swarm-task-list">
            {tasks.map((task) => (
              <button
                key={task.id}
                className="swarm-task-row"
                type="button"
                onClick={() => task.entityId && setSelectedEntityId(task.entityId)}
              >
                <span className="swarm-task-title">{task.title}</span>
                <span className={`swarm-task-status swarm-task-status--${task.status}`}>{task.status}</span>
              </button>
            ))}
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
            {selectedEntity ? <Badge variant="success">Active</Badge> : <Badge variant="neutral">Idle</Badge>}
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
                  <Button size="sm" variant="ghost">
                    Nudge Task
                  </Button>
                </div>
              </>
            ) : (
              <div className="swarm-selection-empty">Select a unit or node on the map to inspect.</div>
            )}
          </div>
        </Card>
      </div>
    </PageShell>
  );
}
