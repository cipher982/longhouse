import { useState } from "react";
import {
  formatTime,
  summarizeApiError,
  summarizeProviderNotice,
  summarizeTaskNotification,
} from "@/shared/session/model";
import type { AgentEvent } from "@/shared/api/agents";

/**
 * A provider notification (a background job finishing, waiting-on-agents status,
 * or API retry notice) as one collapsed row in the Thinking row's vocabulary.
 */
export function ProviderNoticeRow({ event, isSelected }: { event: AgentEvent; isSelected: boolean }) {
  const [expanded, setExpanded] = useState(false);

  if (event.waiting_background_agents != null && event.waiting_background_agents > 0) {
    const count = event.waiting_background_agents;
    const label = `Waiting for ${count} background ${count === 1 ? "agent" : "agents"} to finish`;
    return (
      <div
        id={`event-${event.id}`}
        className={`tl-reasoning tl-reasoning--notice tl-waiting-agents${isSelected ? " is-selected" : ""}`}
        data-testid="session-waiting-background-agents"
        data-row-kind="provider-notification"
      >
        <div className="tl-reasoning__head">
          <span className="tl-reasoning__chev tl-waiting-agents__glyph" aria-hidden="true">*</span>
          <span className="tl-reasoning__label">{label}</span>
          <span className="tl-reasoning__summary" />
          <span className="tl-reasoning__time">{formatTime(event.timestamp)}</span>
        </div>
      </div>
    );
  }

  if (event.task_notification) {
    const summary = summarizeTaskNotification(event.task_notification, event.content_text);
    const bodyId = `provider-notice-body-${event.id}`;
    const hasResult = summary.result !== null;
    const statusClass = summary.status ? ` is-status-${summary.status}` : "";
    const className = `tl-reasoning tl-reasoning--notice tl-task-notice${statusClass}${isSelected ? " is-selected" : ""}${expanded ? " is-expanded" : ""}`;
    const time = <span className="tl-reasoning__time">{formatTime(event.timestamp)}</span>;

    return (
      <div
        id={`event-${event.id}`}
        className={className}
        data-testid="session-task-notification"
        data-row-kind="provider-notification"
      >
        {hasResult ? (
          <button
            type="button"
            className="tl-reasoning__head"
            aria-expanded={expanded}
            aria-controls={bodyId}
            aria-label={`${expanded ? "Collapse" : "Expand"} ${summary.title}`}
            onClick={() => setExpanded((value) => !value)}
          >
            <span className={`tl-reasoning__chev${expanded ? " is-open" : ""}`} aria-hidden="true">›</span>
            <span className="tl-task-notice__dot" aria-hidden="true" />
            <span className="tl-reasoning__label">{summary.title}</span>
            {summary.meta ? <span className="tl-task-notice__meta">{summary.meta}</span> : null}
            {summary.note ? <span className="tl-task-notice__note">{summary.note}</span> : null}
            {time}
          </button>
        ) : (
          <div className="tl-reasoning__head">
            <span className="tl-reasoning__chev" aria-hidden="true">•</span>
            <span className="tl-task-notice__dot" aria-hidden="true" />
            <span className="tl-reasoning__label">{summary.title}</span>
            {summary.meta ? <span className="tl-task-notice__meta">{summary.meta}</span> : null}
            {summary.note ? <span className="tl-task-notice__note">{summary.note}</span> : null}
            {time}
          </div>
        )}
        {summary.entries.length > 1 ? (
          <ul className="tl-task-notice__entries">
            {summary.entries.map((entry, idx) => (
              <li key={`${entry.taskId ?? "item"}-${idx}`} className="tl-task-notice__entry">
                <span className="tl-task-notice__entry-summary">{entry.summary}</span>
                {entry.meta ? <span className="tl-task-notice__entry-meta">{entry.meta}</span> : null}
                {entry.note ? <span className="tl-task-notice__entry-note">{entry.note}</span> : null}
              </li>
            ))}
          </ul>
        ) : null}
        {expanded && summary.result !== null ? (
          <pre
            id={bodyId}
            className="tl-reasoning__body tl-reasoning__output tl-task-notice__result"
            data-testid="session-task-notification-result"
          >
            {summary.result}
          </pre>
        ) : null}
      </div>
    );
  }

  if (event.api_error) {
    const apiNotice = summarizeApiError(event.api_error, event.content_text);
    const bodyId = `provider-notice-body-${event.id}`;
    const hasDetails = apiNotice.detail !== null;
    const className = `tl-reasoning tl-reasoning--notice tl-api-error${isSelected ? " is-selected" : ""}${expanded ? " is-expanded" : ""}`;
    const time = <span className="tl-reasoning__time">{formatTime(event.timestamp)}</span>;

    return (
      <div
        id={`event-${event.id}`}
        className={className}
        data-testid="session-api-error-notice"
        data-row-kind="provider-notification"
      >
        {hasDetails ? (
          <button
            type="button"
            className="tl-reasoning__head"
            aria-expanded={expanded}
            aria-controls={bodyId}
            aria-label={`${expanded ? "Collapse" : "Expand"} ${apiNotice.title}`}
            onClick={() => setExpanded((value) => !value)}
          >
            <span className={`tl-reasoning__chev${expanded ? " is-open" : ""}`} aria-hidden="true">›</span>
            {apiNotice.badge ? <span className="tl-api-error__badge">{apiNotice.badge}</span> : null}
            <span className="tl-reasoning__label">{apiNotice.title}</span>
            {time}
          </button>
        ) : (
          <div className="tl-reasoning__head">
            <span className="tl-reasoning__chev" aria-hidden="true">!</span>
            {apiNotice.badge ? <span className="tl-api-error__badge">{apiNotice.badge}</span> : null}
            <span className="tl-reasoning__label">{apiNotice.title}</span>
            {time}
          </div>
        )}
        {expanded && apiNotice.detail !== null ? (
          <pre id={bodyId} className="tl-reasoning__body tl-reasoning__output" data-testid="session-api-error-detail">
            {apiNotice.detail}
          </pre>
        ) : null}
      </div>
    );
  }

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
