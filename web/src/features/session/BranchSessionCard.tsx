import { useCallback, useRef, useState, type KeyboardEvent } from "react";
import { createSessionBranch } from "@/shared/api/agents";
import { ApiError } from "@/shared/api/base";
import { Button } from "@/shared/ui";

/**
 * What to say about a branch that cannot be offered, or null to say nothing.
 *
 * A reason gets words only when it tells the reader something no other part of
 * the screen already does. Resume's own blockers (offline machine, moved
 * folder, missing contract) are explained once, in the ended-run notice, so
 * repeating them here would be a second, competing explanation. A provider
 * that cannot fork yet is a roadmap fact, not something anyone can act on, and
 * showed on most ended sessions. The approval reasons remain: they are about
 * this particular session, and the answer is to resume it at the machine.
 *
 * iOS keeps the same rule in `branchReasonLabel`.
 */
export function branchUnavailableNote(reason: string | null | undefined): string | null {
  switch (reason) {
    case "permission_mode_unknown":
      return "Longhouse couldn't verify this session's approval settings, so it can't be branched. Resume it in the terminal instead.";
    case "permission_mode_unsupported":
      return "A branch runs without approval prompts and this session ran with them. Resume it in the terminal instead.";
    default:
      return null;
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
  // The server deduplicates on this id, so it has to survive a retry of the
  // same text: after a dropped response the first attempt may have succeeded,
  // and a fresh id would start a second branch. Different text is a different
  // request, and reusing the id for it would be refused.
  const attemptRef = useRef<{ text: string; id: string } | null>(null);

  const submit = useCallback(async () => {
    const text = message.trim();
    if (!text || submitting) return;
    if (attemptRef.current?.text !== text) {
      attemptRef.current = { text, id: crypto.randomUUID() };
    }
    const attempt = attemptRef.current;
    setSubmitting(true);
    setError(null);
    try {
      const branch = await createSessionBranch(sessionId, {
        message: text,
        client_request_id: attempt.id,
      });
      attemptRef.current = null;
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
    const note = branchUnavailableNote(unavailableReason);
    return note ? (
      <p className="branch-session-card__note" data-testid="branch-session-unavailable">
        {note}
      </p>
    ) : null;
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
