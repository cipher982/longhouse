/**
 * How a link target in agent prose renders, shared by the web timeline and the
 * iOS transcript document.
 *
 * Agents point at local files in several spellings:
 *   Antigravity, Claude   [notes.md](file:///Users/me/repo/notes.md#L59-L79)
 *   Codex                 [app.py](/Users/me/repo/app.py:12)
 *   Claude, Cursor, OMP   [README](docs/README.md)
 *   Cursor fences         ```12:15:app/components/Todo.tsx
 * The file is on the agent's machine, so none of these open from a browser or
 * a phone. They render as a short file name with the full path on hover,
 * never as a link that goes nowhere. Only web and mail targets stay links.
 */

export type LinkTarget =
  | { kind: "web"; href: string }
  | { kind: "file"; display: string; title: string }
  | { kind: "text" };

export interface FileTarget {
  path: string;
  /** "12" or "59-79" */
  lines: string | null;
}

const WEB_TARGET = /^(?:https?:\/\/|mailto:)/i;
const ANY_SCHEME = /^[a-z][a-z0-9+.-]*:/i;
const DRIVE_PATH = /^[a-z]:[\\/]/i;
const ABSOLUTE_PATH = /^(?:\/|~\/|[a-z]:[\\/])/i;
const PATH_LIKE_LABEL = /^(?:file:|\/|~\/|[a-z]:[\\/])/i;
const HOME_PREFIX = /^\/(?:Users|home)\/[^/\s]+(?=\/|$)/;
const LINE_FRAGMENT = /^L(\d+)(?:C\d+)?(?:-L?(\d+)(?:C\d+)?)?$/i;
// `app.py:12` or `Makefile:42-50` looks like a scheme to ANY_SCHEME; it is a file and a line.
const BARE_FILE_LINE = /^[^:/?#\s]+:\d+(?:[:-]\d+)?$/;
const LINE_SUFFIX = /:(\d+)(?:([:-])(\d+))?$/;

function lineRange(start: string, end: string | undefined): string {
  return end && end !== start ? `${start}-${end}` : start;
}

function safeDecode(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

export function basename(path: string): string {
  const parts = path.replace(/\\/g, "/").split("/").filter(Boolean);
  return parts[parts.length - 1] ?? path;
}

/** `/Users/me/git/x` and `/home/me/git/x` read as `~/git/x`. */
export function shortenHome(path: string): string {
  return path.replace(HOME_PREFIX, "~");
}

/** A code span that is nothing but a path under a home directory, shortened; else null. */
export function shortenHomeCodePath(text: string): string | null {
  if (!HOME_PREFIX.test(text) || /\s/.test(text)) return null;
  return shortenHome(text);
}

/** A local file reference, or null for web URLs, anchors and other schemes. */
export function parseFileTarget(raw: string): FileTarget | null {
  let s = raw.trim();
  // Protocol-relative `//host/x` is a web address with no scheme, not a path.
  if (!s || s.startsWith("#") || s.startsWith("?") || s.startsWith("//")) return null;
  if (/^file:/i.test(s)) s = s.replace(/^file:(?:\/\/(?:localhost)?)?/i, "");
  else if (ANY_SCHEME.test(s) && !DRIVE_PATH.test(s) && !BARE_FILE_LINE.test(s)) return null;

  let lines: string | null = null;
  const hash = s.indexOf("#");
  if (hash >= 0) {
    const fragment = LINE_FRAGMENT.exec(s.slice(hash + 1));
    if (fragment) lines = lineRange(fragment[1], fragment[2]);
    s = s.slice(0, hash);
  }
  if (!lines) {
    // Codex: `app.py:12`, `app.py:12:3` (line, column) or `app.py:12-20`.
    const suffix = LINE_SUFFIX.exec(s);
    if (suffix) {
      lines = suffix[2] === "-" ? lineRange(suffix[1], suffix[3]) : suffix[1];
      s = s.slice(0, suffix.index);
    }
  }
  s = safeDecode(s);
  if (!s || s === "/") return null;
  return { path: s, lines };
}

export function classifyLinkTarget(href: string | null | undefined, label: string): LinkTarget {
  const raw = (href ?? "").trim();
  if (WEB_TARGET.test(raw)) return { kind: "web", href: raw };
  const file = parseFileTarget(raw);
  if (!file) return { kind: "text" };

  const suffix = file.lines ? `:${file.lines}` : "";
  const shownPath = ABSOLUTE_PATH.test(file.path) ? basename(file.path) : file.path;
  const text = label.trim();
  let display: string;
  if (!text || text === raw || PATH_LIKE_LABEL.test(text)) {
    display = shownPath + suffix;
  } else {
    // The label already names the line when it ends in `:59`, `#L59` or ` L59`.
    const startLine = file.lines?.split("-")[0];
    const named = startLine && new RegExp(`(?:[:#\\s]L?|L)${startLine}(?:\\D|$)`, "i").test(text);
    display = startLine && !named ? text + suffix : text;
  }
  return { kind: "file", display, title: shortenHome(file.path) + suffix };
}

export interface CodeCitation {
  path: string;
  display: string;
  title: string;
}

/** Cursor's code citation fence info string: `12:15:app/components/Todo.tsx`. */
export function parseCodeCitation(info: string | null | undefined): CodeCitation | null {
  // remark keeps only the info string's first word; the iOS parser sees all of it.
  const match = /^(\d+):(\d+):(\S+)/.exec((info ?? "").trim());
  if (!match) return null;
  const lines = lineRange(match[1], match[2]);
  return {
    path: match[3],
    display: `${basename(match[3])}:${lines}`,
    title: `${shortenHome(match[3])}:${lines}`,
  };
}

/** What a link shows as plain text: a file's short name, otherwise its label. */
export function plainLinkText(label: string, href: string): string {
  const target = classifyLinkTarget(href, label);
  return target.kind === "file" ? target.display : label;
}
