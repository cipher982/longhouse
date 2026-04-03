/**
 * LaunchSessionModal - Start a Longhouse session on a connected runner.
 *
 * Supports claude and codex providers. Uses the existing modal pattern from
 * AddRunnerModal and SessionPickerModal.
 */

import { useCallback, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { Button, Spinner } from "./ui";
import {
  launchManagedLocalSession,
  type ManagedLocalProvider,
  type ManagedLocalSessionLaunchRequest,
  type ManagedLocalSessionLaunchResponse,
} from "../services/api/sessionChat";
import type { Runner } from "../services/api";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface LaunchSessionModalProps {
  isOpen: boolean;
  onClose: () => void;
  runner: Runner;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function LaunchSessionModal({
  isOpen,
  onClose,
  runner,
}: LaunchSessionModalProps) {
  const navigate = useNavigate();
  const [provider, setProvider] = useState<ManagedLocalProvider>("claude");
  const [cwd, setCwd] = useState("");
  const [project, setProject] = useState("");
  const [displayName, setDisplayName] = useState("");

  const launchMutation = useMutation<
    ManagedLocalSessionLaunchResponse,
    Error,
    ManagedLocalSessionLaunchRequest
  >({
    mutationFn: launchManagedLocalSession,
  });

  const handleLaunch = useCallback(() => {
    if (!cwd.trim()) return;

    launchMutation.mutate({
      runner_target: `runner:${runner.id}`,
      cwd: cwd.trim(),
      provider,
      project: project.trim() || null,
      display_name: displayName.trim() || null,
    });
  }, [cwd, provider, project, displayName, runner.id, launchMutation]);

  const handleClose = useCallback(() => {
    if (launchMutation.isPending) return;
    launchMutation.reset();
    onClose();
  }, [launchMutation, onClose]);

  if (!isOpen) return null;

  const errorDetail =
    launchMutation.error instanceof Error
      ? launchMutation.error.message
      : launchMutation.error
        ? String(launchMutation.error)
        : null;

  return (
    <div className="modal-overlay" onClick={handleClose}>
      <div
        className="modal-container"
        style={{ maxWidth: 520 }}
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label="Launch Longhouse session"
        data-testid="launch-session-modal"
      >
        <div className="modal-header">
          <h2>Start Longhouse Session</h2>
          <button
            type="button"
            className="modal-close-button"
            onClick={handleClose}
            aria-label="Close"
          >
            &times;
          </button>
        </div>

        <div className="modal-content">
          {launchMutation.isSuccess ? (
            <div className="launch-session-success">
              <p style={{ color: "var(--color-intent-success)", fontWeight: 500, marginBottom: "var(--space-3)" }}>
                Longhouse session launched on {runner.name}
              </p>
              <p style={{ color: "var(--color-text-secondary)", fontSize: "var(--font-size-sm)" }}>
                Open it on the host machine first, then open the session in Longhouse.
              </p>
              <pre className="code-block" style={{ marginTop: "var(--space-2)" }}>
                <code>{launchMutation.data.attach_command}</code>
              </pre>
              {launchMutation.data.provider === "codex" ? (
                <p
                  style={{
                    color: "var(--color-text-secondary)",
                    fontSize: "var(--font-size-sm)",
                    marginTop: "var(--space-3)",
                    marginBottom: 0,
                  }}
                >
                  Codex MVP is terminal-first. Drive the session in the attached Codex terminal while Longhouse
                  mirrors the transcript and runtime state.
                </p>
              ) : null}
              <div className="modal-actions" style={{ marginTop: "var(--space-4)" }}>
                <Button variant="secondary" onClick={handleClose}>
                  Close
                </Button>
                <Button
                  variant="primary"
                  onClick={() => navigate(`/timeline/${launchMutation.data.session_id}`)}
                >
                  Open Session
                </Button>
              </div>
            </div>
          ) : (
            <>
              <p
                style={{
                  color: "var(--color-text-secondary)",
                  fontSize: "var(--font-size-sm)",
                  marginBottom: "var(--space-4)",
                }}
              >
                Start a Longhouse session on <strong>{runner.name}</strong>.
                Longhouse will launch the CLI inside tmux and keep the session available from the timeline.
              </p>
              {provider === "codex" ? (
                <p
                  style={{
                    color: "var(--color-text-secondary)",
                    fontSize: "var(--font-size-sm)",
                    marginBottom: "var(--space-4)",
                  }}
                >
                  Codex is terminal-first in this MVP. Launch here, open it on the host machine, then use Longhouse
                  as the live transcript and runtime view.
                </p>
              ) : null}

              {/* Provider selector */}
              <div style={{ marginBottom: "var(--space-4)" }}>
                <label
                  style={{
                    display: "block",
                    fontSize: "var(--font-size-sm)",
                    fontWeight: 500,
                    color: "var(--color-text-primary)",
                    marginBottom: "var(--space-2)",
                  }}
                >
                  Provider
                </label>
                <div className="install-tabs">
                  <button
                    type="button"
                    className={`install-tab${provider === "claude" ? " install-tab--active" : ""}`}
                    onClick={() => setProvider("claude")}
                  >
                    Claude
                  </button>
                  <button
                    type="button"
                    className={`install-tab${provider === "codex" ? " install-tab--active" : ""}`}
                    onClick={() => setProvider("codex")}
                  >
                    Codex
                  </button>
                </div>
              </div>

              {/* Working directory */}
              <div style={{ marginBottom: "var(--space-4)" }}>
                <label
                  htmlFor="launch-cwd"
                  style={{
                    display: "block",
                    fontSize: "var(--font-size-sm)",
                    fontWeight: 500,
                    color: "var(--color-text-primary)",
                    marginBottom: "var(--space-2)",
                  }}
                >
                  Working directory <span style={{ color: "var(--color-intent-error)" }}>*</span>
                </label>
                <input
                  id="launch-cwd"
                  type="text"
                  className="form-input"
                  placeholder="/home/user/project"
                  value={cwd}
                  onChange={(e) => setCwd(e.target.value)}
                  autoFocus
                  style={{ width: "100%" }}
                />
              </div>

              {/* Project (optional) */}
              <div style={{ marginBottom: "var(--space-4)" }}>
                <label
                  htmlFor="launch-project"
                  style={{
                    display: "block",
                    fontSize: "var(--font-size-sm)",
                    fontWeight: 500,
                    color: "var(--color-text-primary)",
                    marginBottom: "var(--space-2)",
                  }}
                >
                  Project label
                </label>
                <input
                  id="launch-project"
                  type="text"
                  className="form-input"
                  placeholder="(auto-detected from directory name)"
                  value={project}
                  onChange={(e) => setProject(e.target.value)}
                  style={{ width: "100%" }}
                />
              </div>

              {/* Display name (optional) */}
              <div style={{ marginBottom: "var(--space-4)" }}>
                <label
                  htmlFor="launch-display-name"
                  style={{
                    display: "block",
                    fontSize: "var(--font-size-sm)",
                    fontWeight: 500,
                    color: "var(--color-text-primary)",
                    marginBottom: "var(--space-2)",
                  }}
                >
                  Display name
                </label>
                <input
                  id="launch-display-name"
                  type="text"
                  className="form-input"
                  placeholder="(optional)"
                  value={displayName}
                  onChange={(e) => setDisplayName(e.target.value)}
                  style={{ width: "100%" }}
                />
              </div>

              {errorDetail && (
                <div
                  className="ui-action-error-banner"
                  role="alert"
                  style={{ marginBottom: "var(--space-4)" }}
                >
                  {errorDetail}
                </div>
              )}

              <div className="modal-actions">
                <Button variant="secondary" onClick={handleClose} disabled={launchMutation.isPending}>
                  Cancel
                </Button>
                <Button
                  variant="primary"
                  onClick={handleLaunch}
                  disabled={!cwd.trim() || launchMutation.isPending}
                >
                  {launchMutation.isPending ? (
                    <>
                      <Spinner size="sm" /> Launching...
                    </>
                  ) : (
                    "Launch"
                  )}
                </Button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
