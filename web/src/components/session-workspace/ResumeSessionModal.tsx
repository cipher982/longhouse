import { useEffect, useRef, useState } from "react";
import { copyToClipboard } from "../../lib/clipboard";
import type { SessionResumeIntent } from "../../services/api/agents";
import { Button } from "../ui";

export function ResumeSessionModal({
  intent,
  unexpectedStop = false,
  onClose,
}: {
  intent: SessionResumeIntent;
  unexpectedStop?: boolean;
  onClose: () => void;
}) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const [copied, setCopied] = useState(false);
  const machineLabel = intent.machine_label ?? intent.machine_id ?? "its machine";

  useEffect(() => {
    closeRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const copy = async () => {
    if (!intent.command) return;
    if (await copyToClipboard(intent.command)) {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2_000);
    }
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div
        className="modal-container resume-session-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="resume-session-title"
        data-testid="resume-session-modal"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="modal-header">
          <div>
            <h2 id="resume-session-title">Resume on {machineLabel}</h2>
            <p>Continue this Helm in a terminal with the same session and provider thread.</p>
          </div>
          <button ref={closeRef} className="modal-close-button" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>
        <div className="modal-content">
          {unexpectedStop ? (
            <p className="resume-session-modal__recovery" data-testid="resume-session-recovery-copy">
              This Helm stopped unexpectedly. Resume continues from the provider&apos;s last recorded event.
            </p>
          ) : null}
          {intent.available && intent.command ? (
            <>
              <p className="resume-session-modal__copy">
                Run this command on {machineLabel}. Longhouse starts a new Helm run.
              </p>
              <pre className="inspector-code-block" data-testid="resume-session-command">
                <code>{intent.command}</code>
              </pre>
            </>
          ) : (
            <p className="resume-session-modal__copy">
              Resume is no longer available: {formatResumeReason(intent.reason)}.
            </p>
          )}
        </div>
        <div className="modal-actions">
          <Button variant="secondary" onClick={onClose}>Close</Button>
          {intent.available && intent.command ? (
            <Button variant="primary" onClick={() => void copy()}>
              {copied ? "Copied" : "Copy command"}
            </Button>
          ) : null}
        </div>
      </div>
    </div>
  );
}

export function isUnexpectedResumeStop(reason: string | null | undefined): boolean {
  return reason === "provider_exit" || reason === "process_gone";
}

export function formatResumeReason(reason: string | null | undefined): string {
  switch (reason) {
    case "run_active": return "the Helm is still running; use Reattach";
    case "machine_offline": return "the machine is offline";
    case "machine_unknown": return "the machine has not reported current state";
    case "contract_missing": return "the machine no longer has the retained launch contract";
    case "contract_invalid": return "the retained launch contract is invalid";
    case "provider_state_missing": return "the provider's saved thread state is missing";
    case "workspace_mismatch":
    case "workspace_missing": return "the original workspace is unavailable";
    case "provider_identity_mismatch": return "the retained provider thread no longer matches";
    case "provider_incompatible": return "the installed provider CLI no longer matches";
    case "not_helm": return "this was not a Helm session";
    case "unsupported": return "this provider does not support native Helm Resume";
    default: return "eligibility could not be confirmed";
  }
}
