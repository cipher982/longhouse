import { useState } from "react";
import { formatTime, summarizeProviderNotice } from "@/shared/session/model";
import type { AgentEvent } from "@/shared/api/agents";

/**
 * A provider notification (a background job finishing) as one collapsed row in
 * the Thinking row's vocabulary: trace node, label, hint, time. The job's
 * output stays behind a tap, in monospace. A notice that is only its header
 * has nothing to reveal, so it is the same row without a chevron.
 */
export function ProviderNoticeRow({ event, isSelected }: { event: AgentEvent; isSelected: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const { title, hint, body } = summarizeProviderNotice(event.content_text);
  const bodyId = `provider-notice-body-${event.id}`;
  const className = `tl-reasoning tl-reasoning--notice${isSelected ? " is-selected" : ""}${expanded ? " is-expanded" : ""}`;
  const time = <span className="tl-reasoning__time">{formatTime(event.timestamp)}</span>;

  return (
    <div
      id={`event-${event.id}`}
      className={className}
      data-testid="session-provider-notification"
      data-row-kind="provider-notification"
    >
      {body === null ? (
        <div className="tl-reasoning__head">
          <span className="tl-reasoning__chev" aria-hidden="true">•</span>
          <span className="tl-reasoning__label">{title}</span>
          <span className="tl-reasoning__summary" />
          {time}
        </div>
      ) : (
        <button
          type="button"
          className="tl-reasoning__head"
          aria-expanded={expanded}
          aria-controls={bodyId}
          aria-label={`${expanded ? "Collapse" : "Expand"} ${title}`}
          onClick={() => setExpanded((value) => !value)}
        >
          <span className={`tl-reasoning__chev${expanded ? " is-open" : ""}`} aria-hidden="true">›</span>
          <span className="tl-reasoning__label">{title}</span>
          <span className="tl-reasoning__summary">{expanded ? null : hint}</span>
          {time}
        </button>
      )}
      {expanded && body !== null ? (
        <pre id={bodyId} className="tl-reasoning__body tl-reasoning__output" data-testid="session-provider-notification-body">
          {body}
        </pre>
      ) : null}
    </div>
  );
}
