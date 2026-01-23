import { useEffect, useRef, useState } from "react";
import { useCreateEnrollToken } from "../hooks/useRunners";
import { Button, Spinner } from "./ui";

interface AddRunnerModalProps {
  isOpen: boolean;
  onClose: () => void;
}

export default function AddRunnerModal({ isOpen, onClose }: AddRunnerModalProps) {
  const createTokenMutation = useCreateEnrollToken();
  const [copied, setCopied] = useState(false);
  const codeRef = useRef<HTMLPreElement>(null);

  // Generate token when modal opens
  useEffect(() => {
    if (isOpen && !createTokenMutation.data && !createTokenMutation.isPending) {
      createTokenMutation.mutate();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- Only trigger on open, mutation identity changes each render
  }, [isOpen]);

  const handleCopy = () => {
    if (!createTokenMutation.data) return;

    navigator.clipboard.writeText(createTokenMutation.data.docker_command);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const formatExpiry = (expiresAt: string) => {
    const expiry = new Date(expiresAt);
    const now = new Date();
    const diffMs = expiry.getTime() - now.getTime();
    const diffMins = Math.floor(diffMs / 60000);

    if (diffMins <= 0) return "Expired";
    if (diffMins < 60) return `Expires in ${diffMins} minutes`;
    return `Expires in ${Math.floor(diffMins / 60)} hours`;
  };

  if (!isOpen) return null;

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-container" onClick={(e) => e.stopPropagation()}>
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
              <div className="enrollment-info">
                <p className="enrollment-description">
                  Run these commands on your server to register and start a runner:
                </p>
                <p className="enrollment-expiry">
                  {formatExpiry(createTokenMutation.data.expires_at)}
                </p>
              </div>

              <div className="code-block-container">
                <pre ref={codeRef} className="code-block">
                  <code>{createTokenMutation.data.docker_command}</code>
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

              <div className="enrollment-instructions">
                <h3>Instructions:</h3>
                <ol>
                  <li>Copy the commands above</li>
                  <li>Run them on your server (requires Docker)</li>
                  <li>The runner will appear in your runners list once connected</li>
                </ol>
              </div>

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
