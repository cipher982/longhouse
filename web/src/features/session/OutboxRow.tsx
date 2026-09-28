/**
 * One managed input operation owned by the client until the server-authoritative
 * receipt settles it. It renders at the tail of the transcript so text and
 * attachment summaries have one visible owner while delivery is unresolved.
 */

/** `sent` is a brief Runtime Host delivery confirmation, independent of transcript echo. */
export type OutboxEntryState =
  | "sending"
  | "queued"
  | "unconfirmed"
  | "failed"
  | "sent";

export interface OutboxAttachmentSummary {
  filename: string;
  mimeType?: string | null;
  byteSize?: number | null;
}

export interface OutboxEntryAction {
  label: string;
  onClick: () => void;
  disabled?: boolean;
}

export interface OutboxEntry {
  key: string;
  text: string;
  attachments?: OutboxAttachmentSummary[];
  state: OutboxEntryState;
  /** Short reason shown after the state word (failures, drain notices). */
  detail?: string | null;
  /** Warning shown when a legacy row cannot reproduce its original model. */
  warning?: string | null;
  actions?: OutboxEntryAction[];
}

const STATE_LABEL: Record<OutboxEntryState, string> = {
  sending: "Sending…",
  sent: "Sent",
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
      {entry.warning ? (
        <div className="tl-msg__outbox-warning" role="note">
          {entry.warning}
        </div>
      ) : null}
      {entry.text ? (
        <div className="tl-msg__body tl-msg__plain-ask">{entry.text}</div>
      ) : null}
      {entry.attachments && entry.attachments.length > 0 ? (
        <div className="tl-msg__outbox-attachments" aria-label="Attachments">
          {entry.attachments.map((attachment) => (
            <span key={`${attachment.filename}:${attachment.byteSize ?? ""}`}>
              {attachment.filename}
            </span>
          ))}
        </div>
      ) : null}
    </div>
  );
}
