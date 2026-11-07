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


/** Provider reasoning rows carry this prefix on `content_text`. */
export const REASONING_PREFIX = "Thinking:\n";

export function stripReasoningPrefix(text: string): string {
  return text.startsWith(REASONING_PREFIX) ? text.slice(REASONING_PREFIX.length) : text;
}
