import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import {
  ApiError,
  fetchWorkspaceSuggestions,
  launchRemoteSession,
  listMachines,
  type ExecutionLifetime,
  type MachineDirectoryEntry,
  type RemoteSessionLaunchRequest,
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

function providerLifetimes(m: MachineDirectoryEntry | undefined, provider: string): ExecutionLifetime[] {
  return (m?.launch.providers.find((option) => option.provider === provider)?.execution_lifetimes ?? []) as ExecutionLifetime[];
}

function supportsRunOnce(m: MachineDirectoryEntry | undefined, provider: string): boolean {
  return providerLifetimes(m, provider).includes("one_shot");
}

function supportsLiveControl(m: MachineDirectoryEntry | undefined, provider: string): boolean {
  return providerLifetimes(m, provider).includes("live_control");
}

function defaultExecutionLifetime(m: MachineDirectoryEntry | undefined, provider: string): ExecutionLifetime {
  if (m?.launch.default_provider === provider && m.launch.default_execution_lifetime) {
    return m.launch.default_execution_lifetime;
  }
  return supportsRunOnce(m, provider) ? "one_shot" : "live_control";
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

  const cwdInputRef = useRef<HTMLInputElement | null>(null);
  const [deviceId, setDeviceId] = useState<string>("");
  const [provider, setProvider] = useState<string>("");
  const [cwd, setCwd] = useState<string>("");
  const [workspaceSearch, setWorkspaceSearch] = useState<string>("");
  const [executionLifetime, setExecutionLifetime] = useState<ExecutionLifetime>("one_shot");
  const [initialPrompt, setInitialPrompt] = useState<string>("");
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
  const selectedCanRunOnce = supportsRunOnce(selectedMachine, provider);
  const selectedCanLiveControl = supportsLiveControl(selectedMachine, provider);
  const selectedModeSupported =
    executionLifetime === "one_shot" ? selectedCanRunOnce : selectedCanLiveControl;
  const promptRequired = executionLifetime === "one_shot";
  const canSubmit =
    !submitting &&
    !!deviceId &&
    !!provider &&
    !!cwd.trim() &&
    selectedModeSupported &&
    (!promptRequired || !!initialPrompt.trim());

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

  // Keep the execution lifetime valid for the selected machine/provider.
  useEffect(() => {
    if (!isOpen || !selectedMachine || !provider) return;
    if (
      (executionLifetime === "one_shot" && !selectedCanRunOnce) ||
      (executionLifetime === "live_control" && !selectedCanLiveControl)
    ) {
      setExecutionLifetime(defaultExecutionLifetime(selectedMachine, provider));
    }
  }, [isOpen, selectedMachine, provider, executionLifetime, selectedCanRunOnce, selectedCanLiveControl]);

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
    setExecutionLifetime("one_shot");
    setInitialPrompt("");
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
        onClose();
      }
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [isOpen, onClose]);

  // Focus the cwd input once we have a launchable machine selected.
  useEffect(() => {
    if (!isOpen || !deviceId) return;
    cwdInputRef.current?.focus();
  }, [isOpen, deviceId]);

  const handleSubmit = useCallback(async () => {
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      const payload: RemoteSessionLaunchRequest = {
        device_id: deviceId,
        provider,
        cwd: cwd.trim(),
        execution_lifetime: executionLifetime,
        display_name: displayName.trim() || null,
        client_request_id: `launch-${crypto.randomUUID()}`,
      };
      if (executionLifetime === "one_shot") {
        payload.initial_prompt = initialPrompt.trim();
      }
      const result = await launchRemoteSession(payload);
      if (result.launch_state === "launch_failed" || result.launch_state === "launch_orphaned") {
        setError(formatLaunchFailure(result));
        return;
      }
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
  }, [canSubmit, deviceId, provider, cwd, executionLifetime, initialPrompt, displayName, onLaunched]);

  if (!isOpen) return null;

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div
        className="modal-container"
        data-testid="launch-session-modal"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-header">
          <h2>Start Session</h2>
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
              <div className="form-field">
                <span>Machine</span>
                <div className="launch-machine-picker" data-testid="launch-machine-select">
                  <span className="launch-machine-group-label">Available</span>
                  {launchable.map((machine) => (
                    <button
                      key={machine.device_id}
                      type="button"
                      className={`launch-machine-row${machine.device_id === deviceId ? " is-selected" : ""}`}
                      aria-pressed={machine.device_id === deviceId}
                      onClick={() => {
                        setDeviceId(machine.device_id);
                        setProvider("");
                        setExecutionLifetime(machine.launch.default_execution_lifetime ?? "live_control");
                        setInitialPrompt("");
                        setCwd("");
                        setWorkspaceSearch("");
                        setError(null);
                      }}
                    >
                      <span className="launch-machine-status is-ready" aria-hidden="true" />
                      <strong>{machine.machine_name}</strong>
                      <span>Ready</span>
                    </button>
                  ))}
                  {unavailable.length > 0 && (
                    <>
                      <span className="launch-machine-group-label">Unavailable</span>
                      {unavailable.map((machine) => (
                        <div key={machine.device_id} className="launch-machine-row is-unavailable">
                          <span
                            className={`launch-machine-status${machine.launch.blocked_by === "control_down" ? "" : " is-warning"}`}
                            aria-hidden="true"
                          />
                          <strong>{machine.machine_name}</strong>
                          <span>{launchBlockedLabel(machine)}</span>
                        </div>
                      ))}
                    </>
                  )}
                </div>
              </div>

              {selectedMachine && launchProvidersForMachine(selectedMachine).length > 1 && (
                <label className="form-field">
                  <span>Coding agent</span>
                  <select
                    value={provider}
                    onChange={(e) => {
                      setProvider(e.target.value);
                      setError(null);
                    }}
                    data-testid="launch-provider-select"
                  >
                    {launchProvidersForMachine(selectedMachine).map((p) => (
                      <option key={p} value={p}>
                        {getProviderLabel(p)}
                      </option>
                    ))}
                  </select>
                </label>
              )}

              <label className="form-field">
                <span>Working directory on {selectedMachine?.machine_name ?? deviceId}</span>
                <input
                  ref={cwdInputRef}
                  type="text"
                  value={cwd}
                  onChange={(e) => setCwd(e.target.value)}
                  placeholder="/Users/example/git/zerg/longhouse"
                  autoComplete="off"
                  spellCheck={false}
                  data-testid="launch-cwd-input"
                />
                {workspaces.length > 0 && (
                  <input
                    type="text"
                    value={workspaceSearch}
                    onChange={(e) => setWorkspaceSearch(e.target.value)}
                    placeholder="Filter recent workspaces…"
                    autoComplete="off"
                    spellCheck={false}
                    className="launch-workspace-search"
                    data-testid="launch-workspace-search"
                  />
                )}
                {filteredWorkspaces.length > 0 && (
                  <div className="launch-path-suggestions" data-testid="launch-path-suggestions">
                    {filteredWorkspaces.map((w) => (
                      <button
                        key={w.path}
                        type="button"
                        className={`launch-path-chip${w.path === cwd ? " is-selected" : ""}`}
                        title={w.path}
                        onClick={() => {
                          setCwd(w.path);
                          setError(null);
                          cwdInputRef.current?.focus();
                        }}
                      >
                        {w.label}
                      </button>
                    ))}
                  </div>
                )}
                <small>Must be an existing absolute directory on the target machine.</small>
              </label>

              {executionLifetime === "one_shot" && (
                <label className="form-field">
                  <span>First message</span>
                  <textarea
                    value={initialPrompt}
                    onChange={(e) => {
                      setInitialPrompt(e.target.value);
                      setError(null);
                    }}
                    placeholder="What should the agent do?"
                    rows={4}
                    data-testid="launch-initial-prompt"
                  />
                </label>
              )}

              <label className="form-field">
                <span>Display name (optional)</span>
                <input
                  type="text"
                  value={displayName}
                  onChange={(e) => setDisplayName(e.target.value)}
                  placeholder="e.g. zerg — refactor launch"
                  data-testid="launch-display-name"
                />
              </label>

              <details className="launch-advanced" data-testid="launch-advanced-runtime">
                <summary>Advanced</summary>
                <div className="form-field">
                  <span>Runtime</span>
                  <div className="launch-mode-segment" role="radiogroup" aria-label="Runtime">
                    <button
                      type="button"
                      className={`launch-mode-option${executionLifetime === "one_shot" ? " is-selected" : ""}`}
                      aria-pressed={executionLifetime === "one_shot"}
                      disabled={!selectedCanRunOnce}
                      onClick={() => {
                        setExecutionLifetime("one_shot");
                        setError(null);
                      }}
                    >
                      Default
                    </button>
                    <button
                      type="button"
                      className={`launch-mode-option${executionLifetime === "live_control" ? " is-selected" : ""}`}
                      aria-pressed={executionLifetime === "live_control"}
                      disabled={!selectedCanLiveControl}
                      onClick={() => {
                        setExecutionLifetime("live_control");
                        setError(null);
                      }}
                    >
                      Keep runtime open
                    </button>
                  </div>
                </div>
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
                  {submitting ? "Starting…" : "Start"}
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
  const blocked = machines.filter((m) => m.launch.providers.length === 0);
  const offline = blocked.filter((m) => m.launch.blocked_by === "control_down");
  const visibleBlocked = blocked.filter((m) => m.launch.blocked_by !== "control_down").slice(0, 5);
  const hiddenBlockedCount = Math.max(
    blocked.filter((m) => m.launch.blocked_by !== "control_down").length - visibleBlocked.length,
    0,
  );

  return (
    <div className="modal-empty-state" data-testid="launch-no-launchable">
      <p>No machine can start a session right now.</p>
      {visibleBlocked.length > 0 && (
        <ul>
          {visibleBlocked.map((m) => (
            <li key={m.device_id}>
              <strong>{m.machine_name}</strong> — {launchBlockedLabel(m)}
            </li>
          ))}
          {hiddenBlockedCount > 0 && <li>{hiddenBlockedCount} more connected machines are blocked.</li>}
        </ul>
      )}
      {offline.length > 0 && (
        <p>{machineSummary(offline, "have no active control channel")}</p>
      )}
      <p>Restart or upgrade the Machine Agent on the target machine. This sheet refreshes automatically.</p>
    </div>
  );
}

function launchBlockedLabel(machine: MachineDirectoryEntry): string {
  switch (machine.launch.blocked_by) {
    case "control_down":
      return "control channel disconnected";
    case "no_codex_support":
      return "connected, but this engine does not advertise Codex launch";
    case "no_launch_support":
      return "connected, but this engine cannot remote-launch provider sessions";
    case "engine_too_old":
      return "engine too old for Codex launch";
    case "auth_failed":
      return "control channel auth failed";
    case "runtime_unreachable":
      return "runtime host unreachable";
    default:
      return machine.online ? "launch unavailable" : "control channel disconnected";
  }
}

function machineSummary(machines: MachineDirectoryEntry[], reason: string): string {
  const preview = machines.slice(0, 3);
  const hidden = Math.max(machines.length - preview.length, 0);
  const names = preview.map((m) => m.machine_name).join(", ");
  const prefix = machines.length === 1 ? "1 enrolled machine" : `${machines.length} enrolled machines`;
  return `${prefix} ${reason}${names ? `: ${names}` : ""}${hidden > 0 ? `, plus ${hidden} more` : ""}.`;
}

function formatLaunchFailure(result: {
  launch_error_code: string | null;
  launch_error_message: string | null;
}): string {
  const message = result.launch_error_message?.trim();
  if (message) return message;
  const code = result.launch_error_code?.trim();
  if (code) return code;
  return "Launch failed";
}
