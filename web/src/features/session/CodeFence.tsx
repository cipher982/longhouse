import { useEffect, useMemo, useRef, useState } from "react";
import { hasAnsiSequences, parseAnsiSegments } from "./ansiSegments";
import { emberPrismTheme, resolveHighlightLanguage, SyntaxHighlighter } from "./syntaxHighlighter";

export const CODE_HIGHLIGHT_MAX_LINES = 400;
export const CODE_HIGHLIGHT_MAX_CHARS = 20_000;

const EXT_TO_LANG: Record<string, string> = {
  ts: "typescript", tsx: "tsx", js: "javascript", jsx: "jsx", mjs: "javascript",
  cjs: "javascript", py: "python", sh: "bash", bash: "bash", zsh: "bash",
  fish: "bash", json: "json", jsonc: "json", yaml: "yaml", yml: "yaml",
  md: "markdown", markdown: "markdown", css: "css", sql: "sql",
  rs: "rust", go: "go", toml: "toml", diff: "diff", patch: "diff",
  swift: "swift", html: "html", xml: "xml", ini: "ini",
};

export function detectLanguage(
  text: string,
  hint: { filePath?: string | null; toolName?: string } = {},
): string | null {
  const path = hint.filePath?.toLowerCase() ?? "";
  if (path) {
    const ext = path.includes(".") ? path.slice(path.lastIndexOf(".") + 1) : "";
    if (ext && EXT_TO_LANG[ext]) return EXT_TO_LANG[ext];
    if (path.endsWith("/makefile") || path.endsWith("makefile")) return "bash";
  }
  const trimmed = text.trimStart();
  if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
    try {
      JSON.parse(trimmed);
      return "json";
    } catch {
      /* not json */
    }
  }
  if (hint.toolName && /bash|shell|exec/i.test(hint.toolName)) return "bash";
  return null;
}

export interface CodeFenceProps {
  text: string;
  language?: string | null;
  variant?: "prose" | "input" | "output";
  maxHeight?: number;
  wrapLongLines?: boolean;
  /** Render SGR ANSI color codes when falling back to plain pre (e.g. tool output). */
  colorizeAnsi?: boolean;
}

/**
 * Shared code block for both assistant markdown fences and tool-call cards.
 * Provides a header strip with language label + Copy button, Ember-tuned
 * Prism highlighting within the 400-line / 20 KB cap, and optional ANSI
 * color rendering on plain tool outputs.
 */
export function CodeFence({
  text,
  language = null,
  variant = "prose",
  maxHeight,
  wrapLongLines = false,
  colorizeAnsi = false,
}: CodeFenceProps) {
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  const timerRef = useRef<number | null>(null);

  useEffect(
    () => () => {
      if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    },
    [],
  );

  const resolvedLang = resolveHighlightLanguage(language);
  const lineCount = text ? text.split("\n").length : 0;
  const withinCap = text.length <= CODE_HIGHLIGHT_MAX_CHARS && lineCount <= CODE_HIGHLIGHT_MAX_LINES;
  const ansiPresent = colorizeAnsi && hasAnsiSequences(text);
  // Prefer ANSI rendering when tool output carries terminal escape codes.
  const shouldHighlight = Boolean(resolvedLang) && withinCap && !ansiPresent;

  const ansiSegments = useMemo(
    () => (ansiPresent ? parseAnsiSegments(text) : null),
    [ansiPresent, text],
  );

  const handleCopy = (e: React.MouseEvent<HTMLButtonElement>) => {
    e.stopPropagation();
    const clipboard = navigator?.clipboard;
    if (!clipboard?.writeText) {
      setCopyState("failed");
      return;
    }
    clipboard.writeText(text).then(
      () => {
        setCopyState("copied");
        if (timerRef.current !== null) window.clearTimeout(timerRef.current);
        timerRef.current = window.setTimeout(() => setCopyState("idle"), 1600);
      },
      () => {
        setCopyState("failed");
        if (timerRef.current !== null) window.clearTimeout(timerRef.current);
        timerRef.current = window.setTimeout(() => setCopyState("idle"), 2000);
      },
    );
  };

  const isTool = variant !== "prose";

  return (
    <div className={`tl-md-fence${isTool ? " tl-md-fence--tool" : ""}`}>
      <div className="tl-md-fence__header">
        <span className="tl-md-fence__lang">{language || (variant === "output" ? "output" : "text")}</span>
        <button
          type="button"
          className="tl-md-fence__copy"
          aria-label="Copy code"
          onClick={handleCopy}
        >
          {copyState === "copied" ? "Copied" : copyState === "failed" ? "Copy failed" : "Copy"}
        </button>
        <span className="sr-only" aria-live="polite">
          {copyState === "copied" ? "Code copied to clipboard" : copyState === "failed" ? "Copy failed" : ""}
        </span>
      </div>
      <div
        className="tl-md-fence__scroll"
        tabIndex={0}
        style={maxHeight ? { maxHeight, overflowY: "auto" } : undefined}
      >
        {shouldHighlight && resolvedLang ? (
          <SyntaxHighlighter
            language={resolvedLang}
            style={emberPrismTheme}
            customStyle={{
              margin: 0,
              padding: isTool ? "8px 10px" : "10px 12px",
              background: "transparent",
              fontSize: isTool ? "11.5px" : "0.88em",
              lineHeight: 1.5,
              overflow: "visible",
            }}
            codeTagProps={{ style: { fontFamily: "var(--font-family-mono)" } }}
            wrapLongLines={wrapLongLines}
          >
            {text}
          </SyntaxHighlighter>
        ) : (
          <pre className={`tl-md-fence__plain${isTool ? " tl-md-fence__plain--tool" : ""}`}>
            <code>
              {ansiSegments
                ? ansiSegments.map((seg, i) =>
                    seg.className ? (
                      <span key={i} className={seg.className}>
                        {seg.text}
                      </span>
                    ) : (
                      seg.text
                    ),
                  )
                : text}
            </code>
          </pre>
        )}
      </div>
    </div>
  );
}
