/**
 * One managed input operation owned by the client until the server-authoritative
 * receipt settles it. A user's send renders through the transcript's own
 * message row (TimelinePane), keyed by its client request id, so the row the
 * user sees while it is sending is the same element the transcript later
 * fills in: no remount, no second shape. Wake and Longhouse notices render
 * here.
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
  /** The send's idempotency key; the transcript row that echoes it carries
   *  the same id in input_origin, and both render under one React key. */
  clientRequestId?: string | null;
  /** When the user sent it (client clock), for the "You asked at" label. */
  sentAt?: string | null;
  text: string;
  attachments?: OutboxAttachmentSummary[];
  state: OutboxEntryState;
  origin?: "user" | "wake" | "longhouse";
  /** Short reason shown after the state word (failures, drain notices). */
  detail?: string | null;
  /** Warning shown when a legacy row cannot reproduce its original model. */
  warning?: string | null;
  actions?: OutboxEntryAction[];
  /**
   * When set, the entry is a settled receipt the transcript does not show, and
   * it renders at this time among the transcript rows instead of at the tail.
   */
  at?: string | null;
}

export const OUTBOX_STATE_LABEL: Record<OutboxEntryState, string> = {
  sending: "Sending…",
  sent: "Sent",
  queued: "Queued · sends after this turn",
  unconfirmed: "Not confirmed",
  failed: "Not delivered",
};

/** A wake or Longhouse notice; user sends render as transcript message rows. */
export function OutboxRow({ entry }: { entry: OutboxEntry }) {
  if (entry.origin === "wake" || entry.origin === "longhouse") {
    return (
      <div
        className="tl-reasoning tl-reasoning--notice"
        data-testid="session-provider-notification"
        data-origin={entry.origin}
      >
        <div className="tl-reasoning__head">
          <span className="tl-reasoning__chev" aria-hidden="true">
            •
          </span>
          <span className="tl-reasoning__label">{entry.text}</span>
        </div>
      </div>
    );
  }

  return null;
}
