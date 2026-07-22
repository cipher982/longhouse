import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { useQuery } from "@tanstack/react-query";

import {
  ApiError,
  createConsoleSession,
  fetchWorkspaceSuggestions,
  listMachines,
  type MachineDirectoryEntry,
} from "../services/api";
import { Button, Spinner } from "./ui";
import { getProviderLabel } from "../lib/providers";

interface LaunchSessionModalProps {
  isOpen: boolean;
  onClose: () => void;
  onLaunched: (sessionId: string) => void;
}

const WORKSPACE_LIMIT = 12;

function machineCanLaunch(m: MachineDirectoryEntry): boolean {
  return m.launch.providers.length > 0;
}

// Prefer codex for launch-default continuity, else the first advertised provider.
function defaultProvider(m: MachineDirectoryEntry | undefined): string {
  if (!m) return "";
  return m.launch.default_provider ?? "";
}

function launchProvidersForMachine(m: MachineDirectoryEntry): string[] {
  return m.launch.providers.map((option) => option.provider);
}

export default function LaunchSessionModal({
  isOpen,
  onClose,
  onLaunched,
}: LaunchSessionModalProps) {
  const machinesQuery = useQuery({
    queryKey: ["launch-machines"],
    queryFn: listMachines,
    enabled: isOpen,
    refetchOnMount: "always",
    refetchInterval: isOpen ? 5000 : false,
  });

  const machinePickerRef = useRef<HTMLDetailsElement | null>(null);
  const providerPickerRef = useRef<HTMLDetailsElement | null>(null);
  const workspacePickerRef = useRef<HTMLDetailsElement | null>(null);
  const advancedPickerRef = useRef<HTMLDetailsElement | null>(null);
  const [deviceId, setDeviceId] = useState<string>("");
  const [provider, setProvider] = useState<string>("");
  const [cwd, setCwd] = useState<string>("");
  const [workspaceSearch, setWorkspaceSearch] = useState<string>("");
  const [displayName, setDisplayName] = useState<string>("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const machines = useMemo(() => machinesQuery.data?.machines ?? [], [machinesQuery.data]);
  const launchable = useMemo(
    () => machines.filter(machineCanLaunch),
    [machines],
  );
  const unavailable = useMemo(() => machines.filter((machine) => !machineCanLaunch(machine)), [machines]);

  const selectedMachine = launchable.find((m) => m.device_id === deviceId);
  const canSubmit =
    !submitting &&
    !!deviceId &&
    !!provider &&
    !!cwd.trim() &&
    selectedMachine?.launch.providers.some((option) => option.provider === provider);

  const workspacesQuery = useQuery({
    queryKey: ["launch-workspaces", deviceId],
    queryFn: () => fetchWorkspaceSuggestions(deviceId, { limit: WORKSPACE_LIMIT }),
    enabled: isOpen && !!deviceId,
    refetchOnMount: "always",
    staleTime: 15_000,
  });

  const workspaces = useMemo(
    () => workspacesQuery.data?.workspaces ?? [],
    [workspacesQuery.data],
  );

  const filteredWorkspaces = useMemo(() => {
    const q = workspaceSearch.trim().toLowerCase();
    if (!q) return workspaces;
    return workspaces.filter(
      (w) => w.path.toLowerCase().includes(q) || w.label.toLowerCase().includes(q),
    );
  }, [workspaces, workspaceSearch]);

  const handleMachineListKeyDown = useCallback((event: ReactKeyboardEvent<HTMLDivElement>) => {
    const options = Array.from(event.currentTarget.querySelectorAll<HTMLElement>("[role='option']"));
    if (!options.length) return;
    const currentIndex = options.indexOf(document.activeElement as HTMLElement);
    let nextIndex: number | null = null;
    if (event.key === "ArrowDown") nextIndex = currentIndex < 0 ? 0 : Math.min(currentIndex + 1, options.length - 1);
    if (event.key === "ArrowUp") nextIndex = currentIndex < 0 ? options.length - 1 : Math.max(currentIndex - 1, 0);
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = options.length - 1;
    if (nextIndex === null) return;
    event.preventDefault();
    options[nextIndex]?.focus();
  }, []);

  // Auto-select the first launchable machine.
  useEffect(() => {
    if (!isOpen || !launchable.length || deviceId) return;
    setDeviceId(launchable[0].device_id);
  }, [isOpen, launchable, deviceId]);

  // Keep the provider valid for the selected machine.
  useEffect(() => {
    if (!isOpen || !selectedMachine) return;
    const providers = launchProvidersForMachine(selectedMachine);
    if (!provider || !providers.includes(provider)) {
      setProvider(defaultProvider(selectedMachine));
    }
  }, [isOpen, selectedMachine, provider]);

  // Start with the top-ranked workspace the user has actually used on this machine.
  useEffect(() => {
    if (!isOpen || cwd.trim() || workspaces.length === 0) return;
    setCwd(workspaces[0].path);
  }, [isOpen, cwd, workspaces]);

  // Clear state on close.
  useEffect(() => {
    if (isOpen) return;
    setDeviceId("");
    setProvider("");
    setCwd("");
    setWorkspaceSearch("");
    setDisplayName("");
    setSubmitting(false);
    setError(null);
  }, [isOpen]);

  // Esc dismisses the modal.
  useEffect(() => {
    if (!isOpen) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        const openPicker = [workspacePickerRef.current, providerPickerRef.current, machinePickerRef.current, advancedPickerRef.current]
          .find((picker) => picker?.open);
        if (openPicker) {
          openPicker.open = false;
          openPicker.querySelector<HTMLElement>("summary")?.focus();
          return;
        }
        onClose();
      }
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [isOpen, onClose]);

  const handleSubmit = useCallback(async () => {
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      const result = await createConsoleSession({
        device_id: deviceId,
        provider,
        cwd: cwd.trim(),
        display_name: displayName.trim() || null,
        launch_surface: "web",
      });
      onLaunched(result.session_id);
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Launch failed");
      }
    } finally {
      setSubmitting(false);
    }
  }, [canSubmit, deviceId, provider, cwd, displayName, onLaunched]);

  if (!isOpen) return null;

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div
        className="modal-container"
        data-testid="launch-session-modal"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-header">
          <h2>New Session</h2>
          <button
            type="button"
            className="modal-close-button"
            onClick={onClose}
            aria-label="Close"
          >
            ×
          </button>
        </div>

        <div className="modal-content">
          {machinesQuery.isPending ? (
            <div className="modal-loading">
              <Spinner size="md" />
              <p>Loading machines…</p>
            </div>
          ) : machinesQuery.isError ? (
            <p className="text-danger">Failed to load machines.</p>
          ) : launchable.length === 0 ? (
            <EmptyState machines={machinesQuery.data?.machines ?? []} />
          ) : (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void handleSubmit();
              }}
            >
              <span className="launch-section-label">Machine</span>
              <details ref={machinePickerRef} className="launch-choice" data-testid="launch-machine-select">
                <summary aria-haspopup="listbox">
                  <span className="launch-choice-copy">
                    <strong>{selectedMachine?.machine_name ?? "Choose a machine"}</strong>
                    <small><span className="launch-machine-status is-ready" aria-hidden="true" />Ready</small>
                  </span>
                </summary>
                <div className="launch-choice-panel">
                  <span id="launch-available-machines" className="launch-machine-group-label">Available</span>
                  <div className="launch-machine-options" role="listbox" aria-labelledby="launch-available-machines" onKeyDown={handleMachineListKeyDown}>
                    {launchable.map((machine) => (
                      <button
                        key={machine.device_id}
                        type="button"
                        role="option"
                        aria-selected={machine.device_id === deviceId}
                        className={`launch-machine-row${machine.device_id === deviceId ? " is-selected" : ""}`}
                        onClick={() => {
                          const nextProvider = defaultProvider(machine);
                          setDeviceId(machine.device_id);
                          setProvider(nextProvider);
                          setCwd("");
                          setWorkspaceSearch("");
                          setError(null);
                          if (machinePickerRef.current) machinePickerRef.current.open = false;
                        }}
                      >
                        <span className="launch-machine-status is-ready" aria-hidden="true" />
                        <span className="launch-machine-copy"><strong>{machine.machine_name}</strong><small>Ready</small></span>
                        <span>{machine.device_id === deviceId ? "✓" : ""}</span>
                      </button>
                    ))}
                  </div>
                  {unavailable.length > 0 && (
                    <>
                      <span id="launch-unavailable-machines" className="launch-machine-group-label">Unavailable</span>
                      <div className="launch-machine-options" role="group" aria-labelledby="launch-unavailable-machines">
                        {unavailable.map((machine) => (
                          <div
                            key={machine.device_id}
                            className="launch-machine-row is-unavailable"
                            aria-label={`${machine.machine_name}, ${launchBlockedLabel(machine)}, Not available`}
                          >
                            <span className={`launch-machine-status ${machineStatusClass(machine)}`} aria-hidden="true" />
                            <span className="launch-machine-copy"><strong>{machine.machine_name}</strong><small>{launchBlockedLabel(machine)}</small></span>
                            <span />
                          </div>
                        ))}
                      </div>
                    </>
                  )}
                </div>
              </details>

              <span className="launch-section-label">Session</span>
              <div className="launch-session-card">
                {selectedMachine && launchProvidersForMachine(selectedMachine).length > 1 ? (
                  <details ref={providerPickerRef} className="launch-choice launch-choice--nested" data-testid="launch-provider-select">
                    <summary><span className="launch-choice-copy"><strong>{getProviderLabel(provider)}</strong><small>Coding agent</small></span></summary>
                    <div className="launch-choice-panel">
                      {launchProvidersForMachine(selectedMachine).map((p) => (
                        <button key={p} type="button" className="launch-option-row" onClick={() => {
                          setProvider(p);
                          setError(null);
                          if (providerPickerRef.current) providerPickerRef.current.open = false;
                        }}>
                          <span>{getProviderLabel(p)}</span><span>{p === provider ? "✓" : ""}</span>
                        </button>
                      ))}
                    </div>
                  </details>
                ) : (
                  <div className="launch-static-choice"><strong>{getProviderLabel(provider)}</strong><small>Coding agent</small></div>
                )}

                <details ref={workspacePickerRef} className="launch-choice launch-choice--nested">
                  <summary><span className="launch-choice-copy"><strong>{workspaceTitle(cwd, workspaces)}</strong><small>Workspace · {cwd ? compactPath(cwd) : "Choose a workspace"}</small></span></summary>
                  <div className="launch-choice-panel launch-workspace-panel">
                    {workspaces.length > 0 && (
                      <input type="search" value={workspaceSearch} onChange={(e) => setWorkspaceSearch(e.target.value)} placeholder="Filter workspaces…" data-testid="launch-workspace-search" />
                    )}
                    {filteredWorkspaces.map((w) => (
                      <button key={w.path} type="button" className="launch-option-row launch-workspace-row" onClick={() => {
                        setCwd(w.path);
                        setError(null);
                        if (workspacePickerRef.current) workspacePickerRef.current.open = false;
                      }}>
                        <span><strong>{w.label}</strong><small>{compactPath(w.path)}</small></span><span>{w.path === cwd ? "✓" : ""}</span>
                      </button>
                    ))}
                    <label className="launch-manual-path">
                      <span>Other path</span>
                      <input type="text" value={cwd} onChange={(e) => setCwd(e.target.value)} placeholder="/Users/example/git/zerg/longhouse" autoComplete="off" spellCheck={false} data-testid="launch-cwd-input" />
                    </label>
                  </div>
                </details>
              </div>

              <details ref={advancedPickerRef} className="launch-advanced" data-testid="launch-advanced-runtime">
                <summary>Advanced options</summary>
                <label className="form-field">
                  <span>Session name (optional)</span>
                  <input type="text" value={displayName} onChange={(e) => setDisplayName(e.target.value)} placeholder="e.g. zerg — refactor launch" data-testid="launch-display-name" />
                </label>
              </details>

              {error && (
                <p className="text-danger" data-testid="launch-error">
                  {error}
                </p>
              )}

              <div className="modal-actions">
                <Button variant="ghost" onClick={onClose} type="button">
                  Cancel
                </Button>
                <Button
                  variant="primary"
                  type="submit"
                  disabled={!canSubmit}
                  data-testid="launch-submit"
                >
                  {submitting ? "Starting…" : "Start session"}
                </Button>
              </div>
            </form>
          )}
        </div>
      </div>
    </div>
  );
}

function EmptyState({ machines }: { machines: MachineDirectoryEntry[] }) {
  if (machines.length === 0) {
    return (
      <div className="modal-empty-state" data-testid="launch-no-machines">
        <p>No enrolled machines yet.</p>
        <p>
          Install Longhouse on a machine with <code>longhouse connect</code> first. It will show up here once
          it reports in.
        </p>
      </div>
    );
  }
  return (
    <div className="modal-empty-state" data-testid="launch-no-launchable">
      <p><strong>No machines ready to launch</strong></p>
      <p>Your machines remain listed and will become available when their Console connection returns.</p>
      <div className="launch-choice-panel is-static">
        <span className="launch-machine-group-label">Unavailable</span>
        {machines.map((machine) => (
          <div key={machine.device_id} className="launch-machine-row is-unavailable" aria-label={`${machine.machine_name}, ${launchBlockedLabel(machine)}, Not available`}>
            <span className={`launch-machine-status ${machineStatusClass(machine)}`} aria-hidden="true" />
            <span className="launch-machine-copy"><strong>{machine.machine_name}</strong><small>{launchBlockedLabel(machine)}</small></span>
            <span />
          </div>
        ))}
      </div>
    </div>
  );
}

function launchBlockedLabel(machine: MachineDirectoryEntry): string {
  switch (machine.launch.blocked_by) {
    case "control_down":
      return lastSeenLabel(machine);
    case "no_launch_support":
      return "Console launch unavailable";
    case "engine_too_old":
      return "Update required";
    case "auth_failed":
      return "Needs repair";
    case "runtime_unreachable":
      return "Needs repair";
    default:
      return machine.online ? "Console launch unavailable" : lastSeenLabel(machine);
  }
}

function lastSeenLabel(machine: MachineDirectoryEntry): string {
  if (!machine.last_seen_at) return "Offline";
  const seen = new Date(machine.last_seen_at);
  if (Number.isNaN(seen.getTime())) return "Offline";
  const days = Math.max(0, Math.round((Date.now() - seen.getTime()) / 86_400_000));
  if (days === 0) return "Offline · Last seen today";
  return `Offline · Last seen ${days} day${days === 1 ? "" : "s"} ago`;
}

function machineStatusClass(machine: MachineDirectoryEntry): string {
  if (machine.launch.blocked_by === "control_down") return "is-offline";
  if (machine.launch.blocked_by === "auth_failed" || machine.launch.blocked_by === "runtime_unreachable") return "is-repair";
  return "is-warning";
}

function compactPath(path: string): string {
  return path.replace(/^\/Users\/[^/]+/, "~");
}

function workspaceTitle(cwd: string, workspaces: Array<{ path: string; label: string }>): string {
  if (!cwd) return "Choose a workspace";
  return workspaces.find((workspace) => workspace.path === cwd)?.label ?? cwd.split("/").filter(Boolean).at(-1) ?? cwd;
}
