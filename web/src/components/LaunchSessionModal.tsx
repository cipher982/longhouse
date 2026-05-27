import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import {
  ApiError,
  fetchAgentSessions,
  launchRemoteSession,
  listMachines,
  type MachineDirectoryEntry,
  type TimelineSessionCard,
} from "../services/api";
import { Button, Spinner } from "./ui";

interface LaunchSessionModalProps {
  isOpen: boolean;
  onClose: () => void;
  onLaunched: (sessionId: string) => void;
}

const PROVIDER = "codex"; // v1
const RECENT_CWD_LIMIT = 8;

function machineCanLaunch(m: MachineDirectoryEntry): boolean {
  return m.can_launch_codex;
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
  const [cwd, setCwd] = useState<string>("");
  const [displayName, setDisplayName] = useState<string>("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const launchable = useMemo(
    () => machinesQuery.data?.machines.filter(machineCanLaunch) ?? [],
    [machinesQuery.data],
  );

  const recentSessionsQuery = useQuery({
    queryKey: ["launch-recent-cwds", deviceId],
    queryFn: () => fetchAgentSessions({ device_id: deviceId, limit: 50, hide_autonomous: false }),
    enabled: isOpen && !!deviceId,
    refetchOnMount: "always",
    staleTime: 15_000,
  });

  const cwdSuggestions = useMemo(
    () => buildCwdSuggestions(recentSessionsQuery.data?.sessions ?? []),
    [recentSessionsQuery.data],
  );

  // Auto-select the first launchable machine.
  useEffect(() => {
    if (!isOpen || !launchable.length || deviceId) return;
    setDeviceId(launchable[0].device_id);
  }, [isOpen, launchable, deviceId]);

  // Start with a path the user has actually used on this machine.
  useEffect(() => {
    if (!isOpen || cwd.trim() || cwdSuggestions.length === 0) return;
    setCwd(cwdSuggestions[0]);
  }, [isOpen, cwd, cwdSuggestions]);

  // Clear state on close.
  useEffect(() => {
    if (isOpen) return;
    setDeviceId("");
    setCwd("");
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
    if (!deviceId || !cwd.trim()) return;
    setSubmitting(true);
    setError(null);
    try {
      const result = await launchRemoteSession({
        device_id: deviceId,
        provider: PROVIDER,
        cwd: cwd.trim(),
        display_name: displayName.trim() || null,
        client_request_id: `launch-${crypto.randomUUID()}`,
      });
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
  }, [deviceId, cwd, displayName, onLaunched]);

  if (!isOpen) return null;

  const selectedMachine = launchable.find((m) => m.device_id === deviceId);

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
              <label className="form-field">
                <span>Machine</span>
                <select
                  value={deviceId}
                  onChange={(e) => {
                    setDeviceId(e.target.value);
                    setCwd("");
                    setError(null);
                  }}
                  data-testid="launch-machine-select"
                >
                  {launchable.map((m) => (
                    <option key={m.device_id} value={m.device_id}>
                      {m.machine_name} {m.engine_build ? `(${m.engine_build})` : ""}
                    </option>
                  ))}
                </select>
              </label>

              <label className="form-field">
                <span>Working directory on {selectedMachine?.machine_name ?? deviceId}</span>
                <input
                  ref={cwdInputRef}
                  type="text"
                  value={cwd}
                  onChange={(e) => setCwd(e.target.value)}
                  placeholder="/Users/davidrose/git/zerg/longhouse"
                  autoComplete="off"
                  spellCheck={false}
                  data-testid="launch-cwd-input"
                  list="launch-cwd-suggestions"
                />
                <datalist id="launch-cwd-suggestions">
                  {cwdSuggestions.map((path) => (
                    <option key={path} value={path} />
                  ))}
                </datalist>
                {cwdSuggestions.length > 0 && (
                  <div className="launch-path-suggestions" data-testid="launch-path-suggestions">
                    {cwdSuggestions.slice(0, RECENT_CWD_LIMIT).map((path) => (
                      <button
                        key={path}
                        type="button"
                        className="launch-path-chip"
                        onClick={() => {
                          setCwd(path);
                          setError(null);
                          cwdInputRef.current?.focus();
                        }}
                      >
                        {compactPath(path)}
                      </button>
                    ))}
                  </div>
                )}
                <small>Must be an existing absolute directory on the target machine.</small>
              </label>

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
                  disabled={submitting || !deviceId || !cwd.trim()}
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
  const blocked = machines.filter((m) => !m.can_launch_codex);
  const offline = blocked.filter((m) => m.launch_blocked_by === "control_down");
  const visibleBlocked = blocked.filter((m) => m.launch_blocked_by !== "control_down").slice(0, 5);
  const hiddenBlockedCount = Math.max(
    blocked.filter((m) => m.launch_blocked_by !== "control_down").length - visibleBlocked.length,
    0,
  );

  return (
    <div className="modal-empty-state" data-testid="launch-no-launchable">
      <p>No machine can start Codex right now.</p>
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
  switch (machine.launch_blocked_by) {
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

function buildCwdSuggestions(cards: TimelineSessionCard[]): string[] {
  const seen = new Set<string>();
  const paths: string[] = [];

  const add = (path: string | null | undefined) => {
    if (!path || !path.startsWith("/") || seen.has(path)) return;
    seen.add(path);
    paths.push(path);
  };

  for (const card of cards) {
    for (const session of [card.head, card.detail, card.root]) {
      add(session.cwd);
      add(parentPath(session.cwd));
    }
    if (paths.length >= RECENT_CWD_LIMIT * 2) break;
  }

  return paths.slice(0, RECENT_CWD_LIMIT * 2);
}

function parentPath(path: string | null | undefined): string | null {
  if (!path) return null;
  const normalized = path.replace(/\/+$/, "");
  const index = normalized.lastIndexOf("/");
  if (index <= 0) return null;
  const parent = normalized.slice(0, index);
  const parentName = parent.slice(parent.lastIndexOf("/") + 1);
  if (!parentName || parentName === "git") return null;
  return parent;
}

function compactPath(path: string): string {
  return path.replace(/^\/Users\/[^/]+/, "~");
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
