import { useEffect, useRef, useState } from "react";
import { useCreateEnrollToken } from "./useRunners";
import { buildRunnerNativeInstallCommand, describeRunnerNativeInstallMode, type RunnerNativeInstallMode } from "./runnerInstallCommands";
import { parseUTC } from "@/shared/lib/dateUtils";
import ConnectMachine from "@/features/machines/ConnectMachine";
import { Button, Spinner } from "@/shared/ui";

interface AddRunnerModalProps {
  isOpen: boolean;
  onClose: () => void;
}

type InstallTab = "native" | "docker";

/**
 * Connect a machine: its Machine Agent first (that is what imports history
 * and makes it launchable), with the Runner as an optional extra below.
 */
export default function AddRunnerModal({ isOpen, onClose }: AddRunnerModalProps) {
  const createTokenMutation = useCreateEnrollToken();
  const [copied, setCopied] = useState(false);
  const [activeTab, setActiveTab] = useState<InstallTab>("native");
  const [nativeMode, setNativeMode] = useState<RunnerNativeInstallMode>("desktop");
  const [runnerOpen, setRunnerOpen] = useState(false);
  const codeRef = useRef<HTMLPreElement>(null);

  // The Runner is optional, so its enroll token is minted only when asked for.
  useEffect(() => {
    if (runnerOpen && !createTokenMutation.data && !createTokenMutation.isPending) {
      createTokenMutation.mutate();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- Only trigger on expand, mutation identity changes each render
  }, [runnerOpen]);

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
      <div className="modal-container add-runner-modal" data-testid="add-runner-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2>Connect a machine</h2>
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
          <ConnectMachine />

          <details
            className="add-runner-optional"
            data-testid="add-runner-optional"
            onToggle={(e) => setRunnerOpen(e.currentTarget.open)}
          >
            <summary data-testid="add-runner-optional-toggle">Optional: add a Runner</summary>

            <p className="enrollment-description">
              Most machines do not need one. A Runner lets Longhouse run shell commands on a connected machine
              from the browser. It needs <code>python3</code> or Node.
            </p>

            {createTokenMutation.isPending && (
              <div className="modal-loading">
                <Spinner size="lg" />
                <p>Generating Runner command...</p>
              </div>
            )}

            {createTokenMutation.error && (
              <div className="modal-error">
                <p>Failed to create Runner command</p>
                <Button variant="secondary" size="sm" onClick={() => createTokenMutation.mutate()}>
                  Retry
                </Button>
              </div>
            )}

            {createTokenMutation.data && (
              <>
                <div className="install-tabs">
                  <button
                    type="button"
                    className={`install-tab${activeTab === "native" ? " install-tab--active" : ""}`}
                    data-testid="runner-install-tab-native"
                    onClick={() => { setActiveTab("native"); setCopied(false); }}
                  >
                    Native (macOS / Linux)
                  </button>
                  <button
                    type="button"
                    className={`install-tab${activeTab === "docker" ? " install-tab--active" : ""}`}
                    data-testid="runner-install-tab-docker"
                    onClick={() => { setActiveTab("docker"); setCopied(false); }}
                  >
                    Docker
                  </button>
                </div>

                {activeTab === "native" && (
                  <>
                    <div className="install-tabs">
                      <button
                        type="button"
                        className={`install-tab${nativeMode === "desktop" ? " install-tab--active" : ""}`}
                        data-testid="runner-install-mode-desktop"
                        onClick={() => { setNativeMode("desktop"); setCopied(false); }}
                      >
                        Desktop / Laptop
                      </button>
                      <button
                        type="button"
                        className={`install-tab${nativeMode === "server" ? " install-tab--active" : ""}`}
                        data-testid="runner-install-mode-server"
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
                  <pre ref={codeRef} className="code-block" data-testid="add-runner-command">
                    <code>{getCommand()}</code>
                  </pre>
                  <Button
                    variant="secondary"
                    size="sm"
                    className="modal-copy-button"
                    data-testid="add-runner-copy-button"
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
              </>
            )}
          </details>

          <div className="modal-actions">
            <Button variant="primary" onClick={onClose}>
              Done
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}
