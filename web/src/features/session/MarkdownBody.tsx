import { Children, isValidElement, memo, useEffect, useRef, useState, type ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { CodeFence } from "./CodeFence";

function extractNodeText(node: ReactNode): string {
  if (typeof node === "string") return node;
  if (typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(extractNodeText).join("");
  if (isValidElement<{ children?: ReactNode }>(node)) {
    return extractNodeText(node.props.children);
  }
  return "";
}

function MarkdownFence({ children }: { children?: ReactNode }) {
  const firstChild = Children.toArray(children)[0];
  const codeProps = isValidElement<{ className?: string; children?: ReactNode }>(firstChild)
    ? firstChild.props
    : null;
  const rawClass = codeProps?.className ?? "";
  const langMatch = /(?:^|\s)language-([^\s]+)/.exec(rawClass);
  const rawLang = langMatch ? langMatch[1] : null;

  const rawText = codeProps ? extractNodeText(codeProps.children) : extractNodeText(children);
  const codeText = rawText.endsWith("\n") ? rawText.slice(0, -1) : rawText;

  return <CodeFence text={codeText} language={rawLang} variant="prose" />;
}

/** Serialize a rendered HTML table into a clean GFM pipe table for clipboard copy. */
export function serializeTableToMarkdown(tableEl: HTMLTableElement): string {
  const rows = Array.from(tableEl.querySelectorAll("tr"));
  if (rows.length === 0) return "";

  const matrix = rows.map((tr) =>
    Array.from(tr.querySelectorAll("th, td")).map((cell) =>
      (cell.textContent ?? "").replace(/\s+/g, " ").trim().replace(/\|/g, "\\|"),
    ),
  );
  const colCount = Math.max(...matrix.map((r) => r.length));
  if (colCount === 0) return "";

  const padRow = (cells: string[]) => {
    const padded = Array.from({ length: colCount }, (_, i) => cells[i] ?? "");
    return `| ${padded.join(" | ")} |`;
  };

  const lines: string[] = [padRow(matrix[0])];
  lines.push(`| ${Array.from({ length: colCount }, () => "---").join(" | ")} |`);
  for (let i = 1; i < matrix.length; i++) {
    lines.push(padRow(matrix[i]));
  }
  return lines.join("\n");
}

function MarkdownTable({ children }: { children?: ReactNode }) {
  const tableRef = useRef<HTMLTableElement | null>(null);
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  const timerRef = useRef<number | null>(null);

  useEffect(
    () => () => {
      if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    },
    [],
  );

  const handleCopy = (e: React.MouseEvent<HTMLButtonElement>) => {
    e.stopPropagation();
    const tableEl = tableRef.current;
    const clipboard = navigator?.clipboard;
    if (!tableEl || !clipboard?.writeText) {
      setCopyState("failed");
      return;
    }
    const markdown = serializeTableToMarkdown(tableEl);
    clipboard.writeText(markdown).then(
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

  return (
    <div className="tl-msg__table-wrap">
      <div className="tl-msg__table-actions">
        <button
          type="button"
          className="tl-msg__table-copy"
          aria-label="Copy table"
          onClick={handleCopy}
        >
          {copyState === "copied" ? "Copied" : copyState === "failed" ? "Copy failed" : "Copy table"}
        </button>
        <span className="sr-only" aria-live="polite">
          {copyState === "copied" ? "Table copied to clipboard" : copyState === "failed" ? "Copy failed" : ""}
        </span>
      </div>
      <div className="tl-msg__table-scroll">
        <table ref={tableRef}>{children}</table>
      </div>
    </div>
  );
}

const MARKDOWN_PLUGINS = [remarkGfm];

const MARKDOWN_COMPONENTS: Components = {
  a: ({ node: _node, ...props }) => (
    <a {...props} target="_blank" rel="noreferrer noopener" />
  ),
  table: ({ node: _node, children }) => <MarkdownTable>{children}</MarkdownTable>,
  pre: ({ node: _node, children }) => <MarkdownFence>{children}</MarkdownFence>,
};

export const MarkdownBody = memo(function MarkdownBody({ content }: { content: string }) {
  return (
    <ReactMarkdown remarkPlugins={MARKDOWN_PLUGINS} components={MARKDOWN_COMPONENTS}>
      {content}
    </ReactMarkdown>
  );
});
