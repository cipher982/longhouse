/**
 * A message the user sent from Longhouse that the transcript has not echoed
 * yet. It renders in the ask's own place and shape (the tail of the
 * transcript, under a "You" label) so the durable row that
 * replaces it lands without moving anything; only the label carries delivery
 * state.
 */

export type OutboxEntryState = "sending" | "queued" | "unconfirmed" | "failed";

export interface OutboxEntryAction {
  label: string;
  onClick: () => void;
  disabled?: boolean;
}

export interface OutboxEntry {
  key: string;
  text: string;
  state: OutboxEntryState;
  /** Short reason shown after the state word (failures, drain notices). */
  detail?: string | null;
  actions?: OutboxEntryAction[];
}

const STATE_LABEL: Record<OutboxEntryState, string> = {
  sending: "Sending…",
  queued: "Queued · sends after this turn",
  unconfirmed: "Not confirmed",
  failed: "Not delivered",
};

export function OutboxRow({ entry }: { entry: OutboxEntry }) {
  return (
    <div
      className={`tl-msg tl-msg--user tl-msg--outbox tl-msg--outbox-${entry.state}`}
      data-testid="session-outbox-row"
      data-outbox-state={entry.state}
      aria-busy={entry.state === "sending" || undefined}
    >
      <div className="tl-msg__head">
        <span className="tl-msg__node" aria-hidden="true">
          <span className="tl-msg__node-ring" />
        </span>
        <span className="tl-msg__who">You</span>
        <span className="tl-msg__outbox-status" role="status">
          {STATE_LABEL[entry.state]}
          {entry.detail ? (
            <span className="tl-msg__outbox-detail"> — {entry.detail}</span>
          ) : null}
        </span>
        {entry.actions?.map((action) => (
          <button
            key={action.label}
            type="button"
            className="tl-msg__outbox-action"
            onClick={action.onClick}
            disabled={action.disabled}
          >
            {action.label}
          </button>
        ))}
      </div>
      {entry.text ? (
        <div className="tl-msg__body tl-msg__plain-ask">{entry.text}</div>
      ) : null}
    </div>
  );
}
