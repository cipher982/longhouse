import { useEffect, useRef, useState } from "react";
import { useCreateEnrollToken } from "../hooks/useRunners";
import { buildRunnerNativeInstallCommand, describeRunnerNativeInstallMode, type RunnerNativeInstallMode } from "../lib/runnerInstallCommands";
import { parseUTC } from "../lib/dateUtils";
import { Button, Spinner } from "./ui";

interface AddRunnerModalProps {
  isOpen: boolean;
  onClose: () => void;
}

type InstallTab = "native" | "docker";

export default function AddRunnerModal({ isOpen, onClose }: AddRunnerModalProps) {
  const createTokenMutation = useCreateEnrollToken();
  const [copied, setCopied] = useState(false);
  const [activeTab, setActiveTab] = useState<InstallTab>("native");
  const [nativeMode, setNativeMode] = useState<RunnerNativeInstallMode>("desktop");
  const codeRef = useRef<HTMLPreElement>(null);

  // Generate token when modal opens
  useEffect(() => {
    if (isOpen && !createTokenMutation.data && !createTokenMutation.isPending) {
      createTokenMutation.mutate();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- Only trigger on open, mutation identity changes each render
  }, [isOpen]);

  const getCommand = () => {
    if (!createTokenMutation.data) return "";
    return activeTab === "native"
      ? buildRunnerNativeInstallCommand({
          enrollToken: createTokenMutation.data.enroll_token,
          longhouseUrl: createTokenMutation.data.longhouse_url,
          oneLinerInstallCommand: createTokenMutation.data.one_liner_install_command,
        }, nativeMode)
      : createTokenMutation.data.docker_command;
  };

  const handleCopy = () => {
    if (!createTokenMutation.data) return;
    navigator.clipboard.writeText(getCommand());
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const formatExpiry = (expiresAt: string) => {
    const expiry = parseUTC(expiresAt);
    const now = new Date();
    const diffMs = expiry.getTime() - now.getTime();
    const diffMins = Math.floor(diffMs / 60000);

    if (diffMins <= 0) return "Token expired";
    if (diffMins < 60) return `Token expires in ${diffMins} min`;
    return `Token expires in ${Math.floor(diffMins / 60)} hr`;
  };

  if (!isOpen) return null;

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-container add-runner-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2>Add Runner</h2>
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
          {createTokenMutation.isPending && (
            <div className="modal-loading">
              <Spinner size="lg" />
              <p>Generating enrollment token...</p>
            </div>
          )}

          {createTokenMutation.error && (
            <div className="modal-error">
              <p>Failed to create enrollment token</p>
              <Button variant="secondary" size="sm" onClick={() => createTokenMutation.mutate()}>
                Retry
              </Button>
            </div>
          )}

          {createTokenMutation.data && (
            <>
              <p className="enrollment-description">
                Run this on the machine you want to connect. Choose <strong>Desktop / Laptop</strong> for
                personal machines, or <strong>Always-on Linux Server</strong> when the runner should stay
                up after logout and reboot.
              </p>

              <div className="install-tabs">
                <button
                  type="button"
                  className={`install-tab${activeTab === "native" ? " install-tab--active" : ""}`}
                  onClick={() => { setActiveTab("native"); setCopied(false); }}
                >
                  Native (macOS / Linux)
                </button>
                <button
                  type="button"
                  className={`install-tab${activeTab === "docker" ? " install-tab--active" : ""}`}
                  onClick={() => { setActiveTab("docker"); setCopied(false); }}
                >
                  Docker
                </button>
              </div>


              {activeTab === "native" && (
                <>
                  <p className="enrollment-description">
                    Machine type:
                  </p>
                  <div className="install-tabs">
                    <button
                      type="button"
                      className={`install-tab${nativeMode === "desktop" ? " install-tab--active" : ""}`}
                      onClick={() => { setNativeMode("desktop"); setCopied(false); }}
                    >
                      Desktop / Laptop
                    </button>
                    <button
                      type="button"
                      className={`install-tab${nativeMode === "server" ? " install-tab--active" : ""}`}
                      onClick={() => { setNativeMode("server"); setCopied(false); }}
                    >
                      Always-on Linux Server
                    </button>
                  </div>
                  <p className="enrollment-description">
                    {describeRunnerNativeInstallMode(nativeMode)}
                  </p>
                </>
              )}

              <div className="code-block-container">
                <pre ref={codeRef} className="code-block">
                  <code>{getCommand()}</code>
                </pre>
                <Button
                  variant="secondary"
                  size="sm"
                  className="modal-copy-button"
                  onClick={handleCopy}
                  title="Copy to clipboard"
                >
                  {copied ? "Copied!" : "Copy"}
                </Button>
              </div>

              <p className="enrollment-expiry">
                {formatExpiry(createTokenMutation.data.expires_at)}
                {" · "}
                {activeTab === "native"
                  ? nativeMode === "server"
                    ? "Installs as a Linux system service"
                    : "Installs as launchd (macOS) or a Linux user service"
                  : "Runs as a Docker container"}
              </p>

              <div className="modal-actions">
                <Button variant="primary" onClick={onClose}>
                  Done
                </Button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
