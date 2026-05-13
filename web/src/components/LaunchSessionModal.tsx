import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import {
  ApiError,
  launchRemoteSession,
  listMachines,
  type MachineDirectoryEntry,
} from "../services/api";
import { Button, Spinner } from "./ui";

interface LaunchSessionModalProps {
  isOpen: boolean;
  onClose: () => void;
  onLaunched: (sessionId: string) => void;
}

const PROVIDER = "codex"; // v1
const LAUNCH_CAP = `${PROVIDER}.launch`;

function machineCanLaunch(m: MachineDirectoryEntry): boolean {
  return m.online && m.supports.includes(LAUNCH_CAP);
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

  // Auto-select the first launchable machine.
  useEffect(() => {
    if (!isOpen || !launchable.length || deviceId) return;
    setDeviceId(launchable[0].device_id);
  }, [isOpen, launchable, deviceId]);

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
                  onChange={(e) => setDeviceId(e.target.value)}
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
                  placeholder="/Users/you/git/your-repo"
                  autoComplete="off"
                  spellCheck={false}
                  data-testid="launch-cwd-input"
                />
                <small>Must be absolute and under $HOME on the target machine.</small>
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
  const online = machines.filter((m) => m.online);
  const offline = machines.filter((m) => !m.online);
  const offlinePreview = offline.slice(0, 3);
  const hiddenOfflineCount = Math.max(offline.length - offlinePreview.length, 0);

  return (
    <div className="modal-empty-state" data-testid="launch-no-launchable">
      <p>No online machine is advertising Codex launch right now.</p>
      {online.length > 0 && (
        <ul>
          {online.map((m) => (
            <li key={m.device_id}>
              <strong>{m.machine_name}</strong> — online, missing <code>{LAUNCH_CAP}</code>
            </li>
          ))}
        </ul>
      )}
      {offline.length > 0 && (
        <p>
          {offline.length === 1 ? "1 enrolled machine is" : `${offline.length} enrolled machines are`} offline
          {offlinePreview.length > 0 ? `: ${offlinePreview.map((m) => m.machine_name).join(", ")}` : ""}
          {hiddenOfflineCount > 0 ? `, plus ${hiddenOfflineCount} more` : ""}.
        </p>
      )}
      <p>Restart or upgrade the Machine Agent on the target machine, then reopen this sheet.</p>
    </div>
  );
}
