import { useLayoutEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { AgentEvent } from "@/shared/api/agents";

/**
 * The collapsed thought is plain text, not a markdown renderer — a raw
 * "**Updating job commits**" reads as a formatting bug, not emphasis. Strip
 * the common inline markers (links first, so their visible text survives).
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

/**
 * The thought as it reads collapsed: the whole text, markdown stripped, with
 * blank lines folded so a paragraph break costs a line break, not a blank
 * line of the three the clamp allows. The clamp — not a character cap or a
 * first-line cut — decides how much shows, so the finding later in a thought
 * is still on screen when it fits (timeline-reading-experience.md, Change F).
 */
export function thoughtProse(text: string): string {
  return stripMarkdown(text)
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .join("\n");
}

export function reasoningText(event: AgentEvent): string {
  const rawText = event.content_text || "";
  return rawText.startsWith("Thinking:\n") ? rawText.slice("Thinking:\n".length) : rawText;
}

/**
 * A thought leads its step: readable prose, clamped to three lines, the tools
 * it caused trailing beneath it (TimelinePane renders those as trail rows).
 * A click opens the full markdown; the open state is the reader's and nothing
 * else closes it.
 */
export function ReasoningRow({
  event,
  isSelected,
  time = null,
}: {
  event: AgentEvent;
  isSelected: boolean;
  /** Clock label, only when the minute changed since the last thought. */
  time?: string | null;
}) {
  const [expanded, setExpanded] = useState(false);
  const [clamped, setClamped] = useState(true);
  const proseRef = useRef<HTMLSpanElement | null>(null);
  const text = reasoningText(event);
  const prose = thoughtProse(text);
  const bodyId = `reasoning-body-${event.id}`;

  // Only a thought the clamp actually cut offers to open: a click that reveals
  // the same three lines is a control that does nothing.
  useLayoutEffect(() => {
    const node = proseRef.current;
    if (!node || expanded) return;
    // No layout (jsdom, a hidden pane) reports zero; keep the thought openable.
    const measure = () => {
      if (node.clientHeight > 0) setClamped(node.scrollHeight > node.clientHeight + 1);
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, [expanded, prose]);

  const openable = expanded || clamped;

  return (
    <div
      id={`event-${event.id}`}
      className={`tl-thought${isSelected ? " is-selected" : ""}${expanded ? " is-expanded" : ""}${openable ? " is-openable" : ""}`}
      data-testid="session-timeline-reasoning"
      data-row-kind="reasoning"
    >
      {time ? <span className="tl-thought__time">{time}</span> : null}
      {expanded ? (
        <>
          <div id={bodyId} className="tl-thought__body tl-msg__body" data-testid="session-timeline-reasoning-body">
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
          <button
            type="button"
            className="tl-thought__less"
            aria-expanded
            aria-controls={bodyId}
            aria-label="Collapse reasoning"
            onClick={() => setExpanded(false)}
          >
            Show less
          </button>
        </>
      ) : (
        <button
          type="button"
          className="tl-thought__head"
          aria-expanded={false}
          aria-controls={bodyId}
          aria-label="Expand reasoning"
          onClick={() => {
            if (openable) setExpanded(true);
          }}
        >
          <span ref={proseRef} className="tl-thought__prose">
            {prose || "No reasoning details"}
          </span>
        </button>
      )}
    </div>
  );
}
