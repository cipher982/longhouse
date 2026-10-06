/**
 * One-line previews of what a user typed, for rows that show a prompt: the
 * Timeline's subheading, the session rail's turns, the ⌘K switcher.
 *
 * Provider CLIs wrap pasted text, attachments and images in markup
 * (`<pasted_content id="…">`, `<attachment>`, `[Image #1, 906×1500]`) and users
 * often fence a paste in `"""`. Shown raw, a row reads `""" <pasted_content
 * id="268e"> David - …`. This keeps the words the user wrote and names the rest
 * with a short marker. When the prompt is nothing but a paste, the paste's own
 * text is the preview, after a `[pasted]` marker.
 */

const PASTED_RE = /<pasted_content\b[^>]*>([\s\S]*?)(?:<\/pasted_content\b[^>]*>|$)/gi;
const ATTACHMENT_RE = /<attachment\b[^>]*>([\s\S]*?)(?:<\/attachment\s*>|$)/gi;
const IMAGE_RE = /\[Image\s*#\d+[^\]]*\]/gi;
// A closing tag left over after its opener was cut off by a server-side
// truncation, and the bare wrapper tags themselves.
const STRAY_TAG_RE = /<\/?(?:pasted_content|attachment)\b[^>]*>/gi;
const QUOTE_FENCE_RE = /"{2,}|'{3,}|`{3,}/g;
// Box-drawing and block characters: TUI frames pasted from a terminal.
const BOX_RE = /[─-▟]+/g;

// Private-use code points stand in for each wrapper while the text is tidied.
const PASTED = "\uE000";
const ATTACHMENT = "\uE001";
const IMAGE = "\uE002";

const MARKER_LABEL: Record<string, string> = {
  [PASTED]: "[pasted text]",
  [ATTACHMENT]: "[attachment]",
  [IMAGE]: "[image]",
};

function tidy(value: string): string {
  return value
    .replace(STRAY_TAG_RE, " ")
    .replace(QUOTE_FENCE_RE, " ")
    .replace(BOX_RE, " ")
    .replace(/\s+/g, " ")
    .trim();
}

export function cleanPromptPreview(value: string | null | undefined): string {
  if (!value) return "";
  const pasted: string[] = [];
  const attached: string[] = [];
  let text = value
    .replace(PASTED_RE, (_match, inner: string) => {
      pasted.push(inner);
      return ` ${PASTED} `;
    })
    .replace(ATTACHMENT_RE, (_match, inner: string) => {
      attached.push(inner);
      return ` ${ATTACHMENT} `;
    })
    .replace(IMAGE_RE, ` ${IMAGE} `);
  text = tidy(text);

  const typed = tidy(text.replace(/[\uE000-\uE002]/g, " "));
  if (typed) {
    // Repeated markers ("[image] [image]") say nothing more than one.
    return text
      .replace(/([\uE000-\uE002])(?:\s*\1)+/g, "$1")
      .replace(/[\uE000-\uE002]/g, (marker) => MARKER_LABEL[marker])
      .replace(/\s+/g, " ")
      .trim();
  }

  // Nothing typed outside the wrappers: the wrapped text is the prompt.
  const parts = [
    ...pasted.map((inner) => ["[pasted]", tidy(inner)]),
    ...attached.map((inner) => ["[attachment]", tidy(inner)]),
  ]
    .filter(([, inner]) => inner)
    .map(([marker, inner]) => `${marker} ${inner}`);
  if (parts.length > 0) {
    return `${text.includes(IMAGE) ? "[image] " : ""}${parts.join(" ")}`;
  }
  return text.includes(IMAGE) ? "[image]" : "";
}
