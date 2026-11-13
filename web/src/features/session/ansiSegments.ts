export interface AnsiSegment {
  text: string;
  className: string | null;
}

const ANSI_MAX_CHARS = 20_000;
const ANSI_MAX_LINES = 400;

// eslint-disable-next-line no-control-regex
const ESCAPE_ANY_RE = /\x1b(?:\][^\x07\x1b]*(?:\x07|\x1b\\)|\[[0-9;?]*[A-Za-z])/g;
// eslint-disable-next-line no-control-regex
const SGR_TOKEN_RE = /\x1b\[([0-9;]*)m/g;

const FG_CLASS: Record<number, string> = {
  30: "tl-ansi-dim",
  31: "tl-ansi-red",
  32: "tl-ansi-green",
  33: "tl-ansi-yellow",
  34: "tl-ansi-blue",
  35: "tl-ansi-magenta",
  36: "tl-ansi-cyan",
  37: "tl-ansi-white",
  90: "tl-ansi-dim",
  91: "tl-ansi-red",
  92: "tl-ansi-green",
  93: "tl-ansi-yellow",
  94: "tl-ansi-blue",
  95: "tl-ansi-magenta",
  96: "tl-ansi-cyan",
  97: "tl-ansi-white",
};

function map256Color(n: number): string | null {
  if (n >= 0 && n <= 7) return FG_CLASS[30 + n] ?? null;
  if (n >= 8 && n <= 15) return FG_CLASS[90 + (n - 8)] ?? null;
  return null;
}

export function stripAnsi(text: string): string {
  return text.replace(ESCAPE_ANY_RE, "");
}

export function hasAnsiSequences(text: string): boolean {
  return text.includes("\x1b");
}

/**
 * Parse SGR ANSI color sequences into styled spans for tool output.
 * Caps work at 20 KB / 400 lines (falling back to stripped plain text) so
 * huge build logs never create thousands of DOM nodes.
 */
export function parseAnsiSegments(raw: string): AnsiSegment[] {
  if (!hasAnsiSequences(raw)) {
    return [{ text: raw, className: null }];
  }

  if (raw.length > ANSI_MAX_CHARS || raw.split("\n").length > ANSI_MAX_LINES) {
    return [{ text: stripAnsi(raw), className: null }];
  }

  // Strip non-SGR CSI/OSC sequences first (cursor moves, clear line, hyperlinks).
  const cleaned = raw.replace(ESCAPE_ANY_RE, (match) => (match.endsWith("m") && match.startsWith("\x1b[") ? match : ""));

  const segments: AnsiSegment[] = [];
  let fg: string | null = null;
  let bold = false;
  let dim = false;
  let lastIndex = 0;

  const currentClass = () => {
    const parts: string[] = [];
    if (fg) parts.push(fg);
    if (bold) parts.push("tl-ansi-bold");
    if (dim) parts.push("tl-ansi-dim");
    return parts.length > 0 ? parts.join(" ") : null;
  };

  const pushText = (chunk: string) => {
    if (!chunk) return;
    const cls = currentClass();
    const prev = segments[segments.length - 1];
    if (prev && prev.className === cls) {
      prev.text += chunk;
    } else {
      segments.push({ text: chunk, className: cls });
    }
  };

  SGR_TOKEN_RE.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = SGR_TOKEN_RE.exec(cleaned)) !== null) {
    if (match.index > lastIndex) {
      pushText(cleaned.slice(lastIndex, match.index));
    }
    lastIndex = match.index + match[0].length;

    const params = match[1] === "" ? [0] : match[1].split(";").map((n) => (n === "" ? 0 : Number.parseInt(n, 10)));
    for (let i = 0; i < params.length; i++) {
      const code = params[i];
      if (Number.isNaN(code)) continue;
      if (code === 0) {
        fg = null;
        bold = false;
        dim = false;
      } else if (code === 1) {
        bold = true;
      } else if (code === 2) {
        dim = true;
      } else if (code === 22) {
        bold = false;
        dim = false;
      } else if (code === 39) {
        fg = null;
      } else if (FG_CLASS[code]) {
        fg = FG_CLASS[code];
      } else if (code === 38) {
        const mode = params[i + 1];
        if (mode === 5 && i + 2 < params.length) {
          fg = map256Color(params[i + 2]);
          i += 2;
        } else if (mode === 2 && i + 4 < params.length) {
          i += 4;
        }
      } else if (code === 48) {
        const mode = params[i + 1];
        if (mode === 5 && i + 2 < params.length) {
          i += 2;
        } else if (mode === 2 && i + 4 < params.length) {
          i += 4;
        }
      }
    }
  }

  if (lastIndex < cleaned.length) {
    pushText(cleaned.slice(lastIndex));
  }

  return segments.length > 0 ? segments : [{ text: "", className: null }];
}
