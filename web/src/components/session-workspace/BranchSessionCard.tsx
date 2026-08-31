import { useCallback, useState, type KeyboardEvent } from "react";
import { createSessionBranch } from "../../services/api/agents";
import { ApiError } from "../../services/api/base";
import { Button } from "../ui";

/**
 * The reason a branch cannot be offered, in the user's terms.
 *
 * The server sends one served reason and this renders it. Inventing copy per
 * call site is how a refusal ends up explained two different ways on two
 * screens.
 */
function describeUnavailable(reason: string | null | undefined, providerLabel: string): string {
  switch (reason) {
    case "fork_unsupported":
      return `Longhouse can't branch ${providerLabel} sessions yet.`;
    case "permission_mode_unknown":
    case "permission_mode_unsupported":
      return "This session ran with approvals a branch can't carry.";
    case "machine_offline":
    case "machine_unknown":
      return "The machine this ran on is offline.";
    case "contract_missing":
    case "contract_invalid":
      return "The provider state this needs is no longer on the machine.";
    case "workspace_mismatch":
      return "The folder this ran in has moved.";
    default:
      return "This session can't be branched right now.";
  }
}

/**
 * Pick up an ended session from wherever you are.
 *
 * Resume hands back a shell command, which is the right answer at the machine
 * and useless anywhere else. This is the same continuation as a text box: it
 * starts a new session that forks the provider's conversation, so the original
 * stays exactly as it was.
 */
export function BranchSessionCard({
  sessionId,
  providerLabel,
  machineLabel,
  available,
  unavailableReason,
  onBranched,
}: {
  sessionId: string;
  providerLabel: string;
  machineLabel: string;
  available: boolean;
  unavailableReason?: string | null;
  onBranched: (branchSessionId: string) => void;
}) {
  const [message, setMessage] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = useCallback(async () => {
    const text = message.trim();
    if (!text || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      // A stable id per attempt: a retry after a dropped response must not
      // start a second branch, and the server deduplicates on this.
      const branch = await createSessionBranch(sessionId, {
        message: text,
        client_request_id: crypto.randomUUID(),
      });
      setMessage("");
      onBranched(branch.session_id);
    } catch (caught: unknown) {
      setError(caught instanceof ApiError ? caught.message : "Couldn't start the branch");
    } finally {
      setSubmitting(false);
    }
  }, [message, submitting, sessionId, onBranched]);

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      void submit();
    }
  };

  if (!available) {
    return (
      <div className="branch-session-card branch-session-card--unavailable" data-testid="branch-session-unavailable">
        <p>{describeUnavailable(unavailableReason, providerLabel)}</p>
      </div>
    );
  }

  return (
    <div className="branch-session-card" data-testid="branch-session-card">
      <div className="branch-session-card__header">
        <strong>Pick up where this left off</strong>
        <span>
          Starts a new {providerLabel} session on {machineLabel} that continues this conversation. This one stays as it
          is.
        </span>
      </div>
      <textarea
        className="branch-session-card__input"
        data-testid="branch-session-input"
        value={message}
        onChange={(event) => setMessage(event.target.value)}
        onKeyDown={onKeyDown}
        placeholder="What should it do next?"
        rows={3}
        disabled={submitting}
      />
      {error ? (
        <p className="branch-session-card__error" role="alert" data-testid="branch-session-error">
          {error}
        </p>
      ) : null}
      <div className="branch-session-card__actions">
        <Button
          variant="primary"
          size="sm"
          onClick={() => void submit()}
          disabled={submitting || !message.trim()}
          data-testid="branch-session-submit"
        >
          {submitting ? "Starting…" : "Continue here"}
        </Button>
      </div>
    </div>
  );
}
