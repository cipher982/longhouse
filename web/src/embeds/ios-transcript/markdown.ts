import { escapeHtml } from "./escape";

// A deliberately small Markdown subset for assistant prose: fenced code,
// h1-h3, single-line bullets, GFM tables, links, code spans, bold and italic.
// Everything is escaped first, so transcript text never becomes markup.

export function inlineMarkdown(value: unknown): string {
  let html = escapeHtml(value);
  html = html.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" rel="noreferrer noopener">$1</a>');
  html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/\*([^*]+)\*/g, "<em>$1</em>");
  return html;
}

export function paragraphHtml(lines: string[]): string {
  if (!lines.length) return "";
  return "<p>" + inlineMarkdown(lines.join("\n")).replace(/\n/g, "<br>") + "</p>";
}

// Returns true when line looks like a GFM table separator (|---|---|).
// Uses * (not +) for the inner group so single-column |---| also matches.
export function isTableSeparator(line: string): boolean {
  return /^\|?[\s\-:]+(\|[\s\-:]+)*\|?$/.test(line.trim());
}

// Returns true when line looks like a GFM table data row (starts/ends with |,
// or contains at least one | surrounded by non-pipe content).
export function isTableRow(line: string): boolean {
  const t = line.trim();
  return t.startsWith("|") || /\S\|\S/.test(t) || (t.endsWith("|") && t.includes("|"));
}

// Split a pipe-delimited row into trimmed cell strings.
export function splitCells(line: string): string[] {
  const t = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  return t.split("|").map((c) => c.trim());
}

// Parse alignment hints from a separator row.
export function parseAligns(sepLine: string): string[] {
  return splitCells(sepLine).map((cell) => {
    if (cell.startsWith(":") && cell.endsWith(":")) return "center";
    if (cell.endsWith(":")) return "right";
    return "";
  });
}

// Render accumulated table rows (first is header, second is separator) to HTML.
export function tableToHtml(rows: string[]): string {
  // Need at least header + separator; separator must actually look like one.
  if (rows.length < 2 || !isTableSeparator(rows[1])) {
    return rows.map((r) => paragraphHtml([r])).join("");
  }
  const aligns = parseAligns(rows[1]);
  const alignAttr = (i: number) => (aligns[i] ? ` align="${aligns[i]}"` : "");

  const header = splitCells(rows[0]);
  const h = "<thead><tr>" + header.map((c, i) => `<th${alignAttr(i)}>${inlineMarkdown(c)}</th>`).join("") + "</tr></thead>";

  let b = "<tbody>";
  for (let ri = 2; ri < rows.length; ri++) {
    const cells = splitCells(rows[ri]);
    b += "<tr>" + header.map((_, i) => `<td${alignAttr(i)}>${inlineMarkdown(cells[i] ?? "")}</td>`).join("") + "</tr>";
  }
  b += "</tbody>";

  return '<div class="table-wrap"><table>' + h + b + "</table></div>";
}

export function markdownToHtml(value: unknown): string {
  const lines = String(value ?? "").split(/\r?\n/);
  let html = "";
  let paragraph: string[] = [];
  let code: string[] | null = null;
  let tableRows: string[] | null = null; // null = not in table; array = accumulating rows
  let pendingTableLine: string | null = null; // one-line buffer: potential header row

  function flushParagraph() {
    html += paragraphHtml(paragraph);
    paragraph = [];
  }

  function flushCode() {
    if (code !== null) {
      html += "<pre><code>" + escapeHtml(code.join("\n")) + "</code></pre>";
      code = null;
    }
  }

  function flushTable() {
    if (tableRows !== null) {
      html += tableToHtml(tableRows);
      tableRows = null;
    }
  }

  for (const line of lines) {
    const trimmed = line.trim();

    // Code fence — highest priority, swallows everything inside.
    if (trimmed.startsWith("```") || trimmed.startsWith("~~~")) {
      if (code === null) {
        if (pendingTableLine !== null) {
          paragraph.push(pendingTableLine);
          pendingTableLine = null;
        }
        flushParagraph();
        flushTable();
        code = [];
      } else {
        flushCode();
      }
      continue;
    }

    if (code !== null) {
      code.push(line);
      continue;
    }

    // Table accumulation: require header+separator before committing
    // to table mode. Single pipe lines (shell commands, file paths, prose)
    // are buffered for one line and flushed as paragraph text unless a
    // separator immediately follows.
    if (isTableRow(line)) {
      if (tableRows !== null) {
        // Already inside a table — accumulate.
        tableRows.push(line);
        continue;
      }
      // Not yet in a table.
      if (pendingTableLine !== null) {
        // Second pipe line in a row.
        if (isTableSeparator(line)) {
          // Header (buffered) + separator = valid GFM table start.
          flushParagraph();
          tableRows = [pendingTableLine, line];
          pendingTableLine = null;
        } else {
          // Two data rows with no separator — not a table.
          // The buffered line was prose; push it and re-buffer.
          paragraph.push(pendingTableLine);
          pendingTableLine = line;
        }
        continue;
      }
      // First pipe line — buffer as potential header.
      // A separator-only first line is not a valid header; treat as prose.
      if (isTableSeparator(line)) {
        paragraph.push(line);
      } else {
        pendingTableLine = line;
      }
      continue;
    }

    // Non-pipe line: any buffered potential header is prose text.
    if (pendingTableLine !== null) {
      paragraph.push(pendingTableLine);
      pendingTableLine = null;
    }

    // Any non-pipe line breaks an in-progress table.
    if (tableRows !== null) {
      flushTable();
    }

    if (trimmed === "") {
      flushParagraph();
      continue;
    }

    if (trimmed.startsWith("### ")) {
      flushParagraph();
      html += "<h3>" + inlineMarkdown(trimmed.slice(4)) + "</h3>";
      continue;
    }

    if (trimmed.startsWith("## ")) {
      flushParagraph();
      html += "<h2>" + inlineMarkdown(trimmed.slice(3)) + "</h2>";
      continue;
    }

    if (trimmed.startsWith("# ")) {
      flushParagraph();
      html += "<h1>" + inlineMarkdown(trimmed.slice(2)) + "</h1>";
      continue;
    }

    if (trimmed.startsWith("- ") || trimmed.startsWith("* ")) {
      flushParagraph();
      html += "<ul><li>" + inlineMarkdown(trimmed.slice(2)) + "</li></ul>";
      continue;
    }

    paragraph.push(line);
  }

  // Flush any buffered pipe line that was never followed by a separator.
  if (pendingTableLine !== null) {
    paragraph.push(pendingTableLine);
    pendingTableLine = null;
  }

  flushParagraph();
  flushCode();
  flushTable();
  return html;
}
