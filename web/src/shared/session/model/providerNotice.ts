/**
 * A provider notification is a system row the provider itself displays: a
 * background job finishing (OMP/Pi `custom_message`, Claude task notification).
 * Its served text is a header line followed by the job's own output, which
 * can run to 2000 characters. The transcript shows it as one collapsed row,
 * same shape as the Thinking row: the header as the label, the first line of
 * output as the hint, the full text behind a tap.
 *
 * The web timeline and the iOS transcript document both call this, so the two
 * clients cannot disagree about what a notice says.
 */

const TITLE_MAX_CHARS = 120;
const HINT_MAX_CHARS = 90;
/** The server's elision marker between a long output's header and its tail. */
const ELISION_LINE = "…";

export interface ProviderNoticeSummary {
  /** The row label: the header line, without its closing period. */
  title: string;
  /** First line of output plus how many more follow; null for a bare header. */
  hint: string | null;
  /** What expanding reveals, verbatim; null when the title already says it all. */
  body: string | null;
}

function ellipsize(text: string, maxChars: number): string {
  return text.length <= maxChars ? text : `${text.slice(0, maxChars).trimEnd()}…`;
}

export function summarizeProviderNotice(text: string | null | undefined): ProviderNoticeSummary {
  const lines = (text ?? "").replace(/\r\n?/g, "\n").trimEnd().split("\n");
  const headerIndex = lines.findIndex((line) => line.trim().length > 0);
  if (headerIndex === -1) return { title: "Provider update", hint: null, body: null };

  const header = lines[headerIndex].trim().replace(/\.$/, "");
  if (header.length > TITLE_MAX_CHARS) {
    return { title: ellipsize(header, TITLE_MAX_CHARS), hint: null, body: lines.slice(headerIndex).join("\n").trimEnd() };
  }

  const rest = lines.slice(headerIndex + 1);
  const output = rest.filter((line) => line.trim().length > 0);
  if (output.every((line) => line.trim() === ELISION_LINE)) return { title: header, hint: null, body: null };

  // The server slices a long output's tail by characters, so the line right
  // after its elision marker starts mid-line: a fragment, not a preview.
  const elided = output[0].trim() === ELISION_LINE;
  const preview = output.filter((line) => line.trim() !== ELISION_LINE).slice(elided ? 1 : 0);
  const more = preview.length - 1;
  const first = preview.length > 0 ? ellipsize(preview[0].trim(), HINT_MAX_CHARS) : null;
  return {
    title: header,
    hint: first !== null && more > 0 ? `${first} … ${more} more line${more === 1 ? "" : "s"}` : first,
    body: rest.join("\n").replace(/^(?:[ \t]*\n)+/, "").trimEnd(),
  };
}
