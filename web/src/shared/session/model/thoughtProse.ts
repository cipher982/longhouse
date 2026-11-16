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


/**
 * Reasoning is identified by `item.kind === "reasoning"` and its text is the
 * thought itself. The engine stopped writing a literal "Thinking:\n" prefix at
 * parser revision v5, but stored renders from before that (laptop renders that
 * history keeps, and host renders not yet rebuilt) still carry it, so the
 * prefix is stripped wherever it is found. Remove this once no stored render
 * below v5 can be served.
 */
const LEGACY_REASONING_PREFIX = "Thinking:\n";

export function stripLegacyReasoningPrefix(text: string): string {
  return text.startsWith(LEGACY_REASONING_PREFIX) ? text.slice(LEGACY_REASONING_PREFIX.length) : text;
}

/** True when the row is model reasoning: the host's item fact, or the server's classification of an older row without `item`. */
export function isReasoningEvent(event: {
  item?: { kind?: string | null } | null;
  interaction_kind?: string | null;
}): boolean {
  return event.item?.kind === "reasoning" || event.interaction_kind === "provider_reasoning";
}
