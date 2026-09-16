import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { formatTime } from "../../lib/sessionWorkspace";
import type { AgentEvent } from "../../services/api/agents";

const REASONING_COLLAPSE_LINE_LIMIT = 120;
const REASONING_SUMMARY_MAX_CHARS = 90;

/**
 * The collapsed row is a plain text line, not a markdown renderer — a raw
 * "**Updating job commits**" reads as a formatting bug, not emphasis. Strip
 * the common inline markers (links first, so their visible text survives)
 * before anything gets truncated into the one-line preview.
 */
export function stripMarkdown(text: string): string {
  return text
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/^#{1,6}\s+/gm, "")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/__([^_]+)__/g, "$1")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/\*([^*]+)\*/g, "$1")
    .replace(/(^|[^\w])_([^_]+)_(?!\w)/g, "$1$2");
}

function ellipsize(text: string, maxChars: number): string {
  if (text.length <= maxChars) return text;
  return `${text.slice(0, maxChars).trimEnd()}…`;
}

/**
 * The summary is the first non-empty line only, ellipsized — never a join of
 * several lines into one run-on sentence. Joining lines ("...the manual-app
 * workflow Cross-checking the release steps...") reads as one garbled
 * thought instead of "here's where the reasoning starts, expand for more".
 */
export function collapsedPreview(text: string): string {
  if (!text) return "No reasoning details";
  const lines = text.split("\n");
  const firstNonEmptyIndex = lines.findIndex((line) => line.trim().length > 0);
  if (firstNonEmptyIndex === -1) return "No reasoning details";

  const summary = ellipsize(stripMarkdown(lines[firstNonEmptyIndex]).trim(), REASONING_SUMMARY_MAX_CHARS);
  const hiddenLines = lines.length - (firstNonEmptyIndex + 1);
  if (hiddenLines <= 0) return summary;

  const suffix = lines.length > REASONING_COLLAPSE_LINE_LIMIT
    ? `${hiddenLines} lines hidden`
    : `${hiddenLines} more line${hiddenLines === 1 ? "" : "s"}`;
  return `${summary} … ${suffix}`;
}

export function ReasoningRow({ event, isSelected }: { event: AgentEvent; isSelected: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const rawText = event.content_text || "";
  const text = rawText.startsWith("Thinking:\n") ? rawText.slice("Thinking:\n".length) : rawText;
  const bodyId = `reasoning-body-${event.id}`;

  return (
    <div
      id={`event-${event.id}`}
      className={`tl-reasoning${isSelected ? " is-selected" : ""}${expanded ? " is-expanded" : ""}`}
      data-testid="session-timeline-reasoning"
      data-row-kind="reasoning"
    >
      <button
        type="button"
        className="tl-reasoning__head"
        aria-expanded={expanded}
        aria-controls={bodyId}
        aria-label={expanded ? "Collapse reasoning" : "Expand reasoning"}
        onClick={() => setExpanded((value) => !value)}
      >
        <span className={`tl-reasoning__chev${expanded ? " is-open" : ""}`} aria-hidden="true">›</span>
        <span className="tl-reasoning__label">Thinking</span>
        <span className="tl-reasoning__summary">{expanded ? "Reasoning details" : collapsedPreview(text)}</span>
        <span className="tl-reasoning__time">{formatTime(event.timestamp)}</span>
      </button>
      {expanded ? (
        <div id={bodyId} className="tl-reasoning__body tl-msg__body" data-testid="session-timeline-reasoning-body">
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            components={{
              a: ({ node: _node, ...props }) => (
                <a {...props} target="_blank" rel="noreferrer noopener" />
              ),
            }}
          >
            {text}
          </ReactMarkdown>
        </div>
      ) : null}
    </div>
  );
}
