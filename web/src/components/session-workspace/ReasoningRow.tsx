import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { formatTime } from "../../lib/sessionWorkspace";
import type { AgentEvent } from "../../services/api/agents";

const REASONING_COLLAPSE_LINE_LIMIT = 120;
const REASONING_PREVIEW_LINE_LIMIT = 2;

function collapsedPreview(text: string): string {
  if (!text) return "No reasoning details";
  const lines = text.split("\n");
  const previewLineCount = Math.min(REASONING_PREVIEW_LINE_LIMIT, lines.length);
  const preview = lines.slice(0, previewLineCount).join(" ").trim();
  const hiddenLines = lines.length - previewLineCount;
  if (hiddenLines === 0) return preview;

  const suffix = lines.length > REASONING_COLLAPSE_LINE_LIMIT
    ? `${hiddenLines} lines hidden`
    : `${hiddenLines} more line${hiddenLines === 1 ? "" : "s"}`;
  return `${preview} … ${suffix}`;
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
