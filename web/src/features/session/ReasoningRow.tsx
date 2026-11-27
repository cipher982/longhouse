import { useLayoutEffect, useState } from "react";
import { RelativeTime } from "@/shared/ui/RelativeTime";
import type { AgentEvent } from "@/shared/api/agents";

import { MarkdownBody } from "./MarkdownBody";
import { useLiveThinkingExpanded, useLiveThinkingHotkey, LIVE_THINKING_TOGGLE_KEY } from "./liveThinkingPreference";
import { stripLegacyReasoningPrefix, stripMarkdown, thoughtProse } from "@/shared/session/model/thoughtProse";

export { stripMarkdown, thoughtProse };

export function reasoningText(event: AgentEvent): string {
  return stripLegacyReasoningPrefix(event.content_text || "");
}

/**
 * A thought leads its step: readable prose, clamped to three lines, the tools
 * it caused trailing beneath it (TimelinePane renders those as trail rows).
 * A click opens the full markdown; the open state is the reader's and nothing
 * else closes it.
 */
function isLivePreviewReasoning(event: AgentEvent): boolean {
  return typeof event.id === "number" && event.id <= 0;
}

/**
 * Thinking that is still arriving (a live preview, not a stored thought):
 * one "Thinking" line, closed until the viewer opens it by click or by the
 * toggle key; the choice is remembered and applies to every live thinking row.
 */
function LiveThinkingRow({ event, isSelected }: { event: AgentEvent; isSelected: boolean }) {
  const [expanded, setExpanded] = useLiveThinkingExpanded();
  useLiveThinkingHotkey(expanded, setExpanded);
  const text = reasoningText(event);
  const bodyId = `reasoning-body-${event.id}`;
  return (
    <div
      id={`event-${event.id}`}
      className={`tl-thought is-live${isSelected ? " is-selected" : ""}${expanded ? " is-expanded" : ""}`}
      data-testid="session-timeline-reasoning"
      data-row-kind="reasoning"
      data-live="true"
    >
      <button
        type="button"
        className="tl-thought__live"
        aria-expanded={expanded}
        aria-controls={bodyId}
        aria-keyshortcuts={LIVE_THINKING_TOGGLE_KEY}
        aria-label={expanded ? "Collapse thinking" : "Expand thinking"}
        onClick={() => setExpanded(!expanded)}
      >
        <span className="tl-thought__live-label">Thinking</span>
        <span className="tl-thought__live-hint" aria-hidden="true">
          {expanded ? "hide" : "show"} ({LIVE_THINKING_TOGGLE_KEY})
        </span>
      </button>
      {expanded ? (
        <div id={bodyId} className="tl-thought__body tl-msg__body" data-testid="session-timeline-reasoning-body">
          <MarkdownBody content={text} />
        </div>
      ) : null}
    </div>
  );
}

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
  const cleaned = reasoningText(event).trim();
  const isWithheld = Boolean(event.reasoning_withheld) || event.item?.visibility === "withheld";
  if (isWithheld && (!cleaned || cleaned === "[Reasoning withheld]")) {
    return (
      <div
        id={`event-${event.id}`}
        className={`tl-thought tl-thought--withheld${isSelected ? " is-selected" : ""}`}
        data-testid="session-reasoning-withheld"
        data-row-kind="reasoning"
      >
        {time ? <RelativeTime iso={time} className="tl-thought__time" /> : null}
        <div className="tl-thought__head">
          <span className="tl-thought__prose tl-thought__prose--withheld">Reasoning withheld</span>
        </div>
      </div>
    );
  }
  if (isLivePreviewReasoning(event)) return <LiveThinkingRow event={event} isSelected={isSelected} />;
  return <StoredReasoningRow event={event} isSelected={isSelected} time={time} />;
}

function StoredReasoningRow({
  event,
  isSelected,
  time = null,
}: {
  event: AgentEvent;
  isSelected: boolean;
  time?: string | null;
}) {
  const [expanded, setExpanded] = useState(false);
  const [clamped, setClamped] = useState(true);
  // State, not a ref: the prose span remounts when the row switches between
  // its button and text forms, and the observer has to follow the new node.
  const [proseNode, setProseNode] = useState<HTMLSpanElement | null>(null);
  const text = reasoningText(event);
  const prose = thoughtProse(text);
  const bodyId = `reasoning-body-${event.id}`;

  // Only a thought the clamp actually cut offers to open: a click that reveals
  // the same three lines is a control that does nothing.
  useLayoutEffect(() => {
    const node = proseNode;
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
  }, [expanded, prose, proseNode]);

  const openable = expanded || clamped;

  return (
    <div
      id={`event-${event.id}`}
      className={`tl-thought${isSelected ? " is-selected" : ""}${expanded ? " is-expanded" : ""}${openable ? " is-openable" : ""}`}
      data-testid="session-timeline-reasoning"
      data-row-kind="reasoning"
    >
      {time ? <RelativeTime iso={time} className="tl-thought__time" /> : null}
      {expanded ? (
        <>
          <div id={bodyId} className="tl-thought__body tl-msg__body" data-testid="session-timeline-reasoning-body">
            <MarkdownBody content={text} />
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
      ) : openable ? (
        <button
          type="button"
          className="tl-thought__head"
          aria-expanded={false}
          aria-label="Expand reasoning"
          onClick={() => setExpanded(true)}
        >
          <span ref={setProseNode} className="tl-thought__prose">
            {prose || "No reasoning details"}
          </span>
        </button>
      ) : (
        // A thought that fits its three lines is text, not a control.
        <div className="tl-thought__head">
          <span ref={setProseNode} className="tl-thought__prose">
            {prose || "No reasoning details"}
          </span>
        </div>
      )}
    </div>
  );
}
