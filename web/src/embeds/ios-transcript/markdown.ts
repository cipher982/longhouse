import { escapeHtml } from "./escape";
import {
  classifyLinkTarget,
  parseCodeCitation,
  shortenHomeCodePath,
} from "../../shared/session/model/linkTargets";

// Markdown for assistant prose, rendered to an HTML string. It covers what
// agents write (fences, headings, nested and ordered lists, task items, block
// quotes, rules, GFM tables, links, code spans, emphasis, strikethrough, bare
// URLs) and is a hand-rolled single pass because a cold open renders every row:
// micromark measured about 60x slower and marked about 12x slower on real
// transcripts. Transcript text is escaped as it is emitted, so it never
// becomes markup.

// ---------------------------------------------------------------- inline

// A private-use character brackets the index of a held-out chunk of HTML.
const HOLD = "\uE000";
const ESCAPABLE = /[!-/:-@[-`{-~]/;
const BARE_URL = /^https?:\/\/[^\s<>]+/;
// The characters that can start an inline construct; text between them is copied in one slice.
const INLINE_SPECIAL = /[\\`[!<]|https?:\/\//g;
const CODE_SPECIAL = /[\\`]/g;
const AUTOLINK = /^<((?:https?:\/\/|mailto:|file:)[^\s<>]+)>/i;

function codeSpanHtml(body: string): string {
  let text = body.replace(/\n/g, " ");
  if (text.length > 2 && text.startsWith(" ") && text.endsWith(" ") && text.trim()) text = text.slice(1, -1);
  const short = shortenHomeCodePath(text);
  return short
    ? `<code title="${escapeHtml(text)}">${escapeHtml(short)}</code>`
    : `<code>${escapeHtml(text)}</code>`;
}

function linkHtml(label: string, target: string, image = false): string {
  let resolved = classifyLinkTarget(target, label.replace(/[`*]|~~/g, ""));
  // An image the viewer cannot load is named by its file, not its caption.
  if (image && resolved.kind === "file") resolved = classifyLinkTarget(target, "");
  if (resolved.kind === "web") {
    return `<a href="${escapeHtml(resolved.href)}" rel="noreferrer noopener">${renderInline(label, false)}</a>`;
  }
  if (resolved.kind === "file") {
    return `<span class="file-ref" title="${escapeHtml(resolved.title)}">${escapeHtml(resolved.display)}</span>`;
  }
  return renderInline(label, false);
}

interface LinkMatch {
  end: number;
  label: string;
  target: string;
}

/** `[label](target "title")` starting at `start` (the `[`), with nested brackets and balanced parens. */
function scanLink(s: string, start: number): LinkMatch | null {
  let depth = 0;
  let i = start;
  for (; i < s.length; i++) {
    const c = s[i];
    if (c === "\\") {
      i++;
    } else if (c === "`") {
      // A code span in the label hides its brackets: [`arr[0]`](x).
      let n = 1;
      while (s[i + n] === "`") n++;
      const close = s.indexOf(s.slice(i, i + n), i + n);
      if (close >= 0) i = close + n - 1;
      else i += n - 1;
    } else if (c === "[") {
      depth++;
    } else if (c === "]") {
      depth--;
      if (depth === 0) break;
    } else if (c === "\n" && s[i + 1] === "\n") {
      return null;
    }
  }
  if (i >= s.length || s[i + 1] !== "(") return null;
  const label = s.slice(start + 1, i);
  let j = i + 2;
  while (s[j] === " ") j++;
  let target: string;
  if (s[j] === "<") {
    const close = s.indexOf(">", j);
    if (close < 0 || s.slice(j, close).includes("\n")) return null;
    target = s.slice(j + 1, close);
    j = close + 1;
  } else {
    const from = j;
    let parens = 0;
    for (; j < s.length; j++) {
      const c = s[j];
      if (c === "\\") j++;
      else if (c === "(") parens++;
      else if (c === ")") {
        if (parens === 0) break;
        parens--;
      } else if (c === " " || c === "\n" || c === "\t") break;
    }
    target = s.slice(from, j);
  }
  while (s[j] === " ") j++;
  if (s[j] === '"' || s[j] === "'") {
    const close = s.indexOf(s[j], j + 1);
    if (close < 0) return null;
    j = close + 1;
    while (s[j] === " ") j++;
  }
  if (s[j] !== ")") return null;
  return { end: j + 1, label, target };
}

/** A bare URL's end, leaving trailing sentence punctuation and an unmatched `)` outside it. */
function trimBareUrl(url: string): string {
  let end = url.length;
  for (;;) {
    const last = url[end - 1];
    if (/[.,;:!?'"*_~]/.test(last)) {
      end--;
      continue;
    }
    if (last === ")") {
      const body = url.slice(0, end);
      if ((body.match(/\(/g) || []).length < (body.match(/\)/g) || []).length) {
        end--;
        continue;
      }
    }
    return url.slice(0, end);
  }
}

// Emphasis runs on escaped text whose code spans, links and escapes are
// already held out of reach, so `*` inside code or a URL never styles.
function emphasis(html: string): string {
  return html
    .replace(/\*\*\*([^\s*](?:[^*]*?[^\s*])?)\*\*\*/g, "<strong><em>$1</em></strong>")
    // Bold ending or starting in italic: **foo *bar*** and ***bar* foo**.
    .replace(/\*\*([^\s*][^*]*?)\*([^\s*](?:[^*]*?[^\s*])?)\*\*\*/g, "<strong>$1<em>$2</em></strong>")
    .replace(/\*\*\*([^\s*](?:[^*]*?[^\s*])?)\*([^*]*?[^\s*])\*\*/g, "<strong><em>$1</em>$2</strong>")
    .replace(/\*\*([^\s*](?:[\s\S]*?[^\s*])?)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^\w])__([^\s_](?:[\s\S]*?[^\s_])?)__(?!\w)/g, "$1<strong>$2</strong>")
    .replace(/\*([^\s*](?:[^*\n]*?[^\s*])?)\*/g, "<em>$1</em>")
    .replace(/(^|[^\w])_([^\s_](?:[^_\n]*?[^\s_])?)_(?!\w)/g, "$1<em>$2</em>")
    .replace(/~~([^\s~](?:[^~\n]*?[^\s~])?)~~/g, "<del>$1</del>");
}

function renderInline(source: string, allowLinks: boolean): string {
  const s = source.replace(/\uE000/g, "\uFFFD");
  const held: string[] = [];
  let out = "";
  let plain = "";
  const emit = (html: string) => {
    out += escapeHtml(plain) + HOLD + (held.push(html) - 1) + HOLD;
    plain = "";
  };

  const special = allowLinks ? INLINE_SPECIAL : CODE_SPECIAL;
  special.lastIndex = 0;
  let i = 0;
  while (i < s.length) {
    special.lastIndex = i;
    const found = special.exec(s);
    if (!found) {
      plain += s.slice(i);
      break;
    }
    plain += s.slice(i, found.index);
    i = found.index;
    const c = s[i];
    if (c === "\\") {
      if (ESCAPABLE.test(s[i + 1] ?? "")) {
        emit(escapeHtml(s[i + 1]));
        i += 2;
      } else {
        plain += c;
        i++;
      }
      continue;
    }
    if (c === "`") {
      let n = 1;
      while (s[i + n] === "`") n++;
      const ticks = s.slice(i, i + n);
      let k = i + n;
      let close = -1;
      while ((k = s.indexOf(ticks, k)) >= 0) {
        if (s[k + n] !== "`") {
          close = k;
          break;
        }
        while (s[k] === "`") k++;
      }
      if (close < 0) {
        plain += ticks;
        i += n;
        continue;
      }
      emit(codeSpanHtml(s.slice(i + n, close)));
      i = close + n;
      continue;
    }
    if (c === "[" || (c === "!" && s[i + 1] === "[")) {
      const image = c === "!";
      const link = scanLink(s, image ? i + 1 : i);
      if (link) {
        emit(linkHtml(image ? link.label || "image" : link.label, link.target, image));
        i = link.end;
        continue;
      }
    } else if (c === "<") {
      const auto = AUTOLINK.exec(s.slice(i, i + 2048));
      if (auto) {
        emit(linkHtml(auto[1], auto[1]));
        i += auto[0].length;
        continue;
      }
    } else if (c === "h" && (i === 0 || !/[\w/]/.test(s[i - 1]))) {
      const bare = BARE_URL.exec(s.slice(i, i + 2048));
      if (bare) {
        const url = trimBareUrl(bare[0]);
        if (url.length > 8) {
          emit(`<a href="${escapeHtml(url)}" rel="noreferrer noopener">${escapeHtml(url)}</a>`);
          i += url.length;
          continue;
        }
      }
    }
    plain += c;
    i++;
  }
  out += escapeHtml(plain);
  return emphasis(out).replace(/\uE000(\d+)\uE000/g, (_, n: string) => held[Number(n)]);
}

export function inlineMarkdown(value: unknown): string {
  return renderInline(String(value ?? ""), true);
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
  const t = line.trim().replace(/^\|/, "").replace(/(?<!\\)\|$/, "");
  // Split on pipes that are neither escaped (\|) nor inside a code span.
  const cells: string[] = [];
  let cell = "";
  for (let i = 0; i < t.length; i++) {
    const c = t[i];
    if (c === "\\" && t[i + 1] === "|") {
      cell += "|";
      i++;
    } else if (c === "`") {
      let n = 1;
      while (t[i + n] === "`") n++;
      const close = t.indexOf(t.slice(i, i + n), i + n);
      const end = close < 0 ? i + n : close + n;
      cell += t.slice(i, end).replace(/\\\|/g, "|");
      i = end - 1;
    } else if (c === "|") {
      cells.push(cell.trim());
      cell = "";
    } else {
      cell += c;
    }
  }
  cells.push(cell.trim());
  return cells;
}

// Parse alignment hints from a separator row.
export function parseAligns(sepLine: string): string[] {
  return splitCells(sepLine).map((cell) => {
    if (cell.startsWith(":") && cell.endsWith(":")) return "center";
    if (cell.endsWith(":")) return "right";
    return "";
  });
}

// A cell longer than this is a sentence that wraps; a shorter one is a label
// or a value and stays on one line.
const PROSE_CELL_CHARS = 20;

// Render accumulated table rows (first is header, second is separator) to HTML.
export function tableToHtml(rows: string[]): string {
  // Need at least header + separator; separator must actually look like one.
  if (rows.length < 2 || !isTableSeparator(rows[1])) {
    return rows.map((r) => paragraphHtml([r])).join("");
  }
  const aligns = parseAligns(rows[1]);
  const header = splitCells(rows[0]).map(inlineMarkdown);
  const body = rows.slice(2).map((row) => {
    const cells = splitCells(row);
    return header.map((_, i) => inlineMarkdown(cells[i] ?? ""));
  });
  // Width follows the text a cell shows, not its markup or a link's address.
  const shown = (cell: string) => cell.replace(/<[^>]*>/g, "").length;
  const prose = header.map((_, i) => [header, ...body].some((cells) => shown(cells[i]) > PROSE_CELL_CHARS));
  const cellAttrs = (i: number) => (prose[i] ? ' class="prose"' : "") + (aligns[i] ? ` align="${aligns[i]}"` : "");

  const h = "<thead><tr>" + header.map((c, i) => `<th${cellAttrs(i)}>${c}</th>`).join("") + "</tr></thead>";

  let b = "<tbody>";
  for (const cells of body) {
    b += "<tr>" + cells.map((c, i) => `<td${cellAttrs(i)}>${c}</td>`).join("") + "</tr>";
  }
  b += "</tbody>";

  return '<div class="table-wrap"><table>' + h + b + "</table></div>";
}

// ---------------------------------------------------------------- blocks

const FENCE_OPEN = /^(\s*)(`{3,}|~{3,})(.*)$/;
const FENCE_CLOSE = /^\s*(`{3,}|~{3,})\s*$/;
const HEADING = /^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$/;
const RULE = /^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/;
const LIST_ITEM = /^( *)([-*+]|\d{1,9}[.)])(?:([ \t]+)(.*))?$/;
const QUOTE = /^ {0,3}> ?(.*)$/;
const TASK = /^\[([ xX])\][ \t]+/;

function fenceOpen(line: string): RegExpExecArray | null {
  const open = FENCE_OPEN.exec(line);
  if (!open) return null;
  // A backtick fence's info string has no backticks; ```x``` is a code span.
  if (open[2][0] === "`" && open[3].includes("`")) return null;
  return open;
}

function listItem(line: string): RegExpExecArray | null {
  return LIST_ITEM.exec(line);
}

/** A list item that may end a paragraph. An ordered one must start at 1, so a
 * sentence that wraps onto "2026. That…" stays prose. */
function interruptingItem(line: string): RegExpExecArray | null {
  const item = listItem(line);
  return item && item[3] !== undefined && (!/\d/.test(item[2]) || parseInt(item[2], 10) === 1) ? item : null;
}

function startsBlock(line: string): boolean {
  return !!(fenceOpen(line) || HEADING.test(line) || RULE.test(line) || QUOTE.test(line) || interruptingItem(line));
}

function fenceHtml(body: string, info: string): string {
  const citation = parseCodeCitation(info);
  const caption = citation
    ? `<div class="code-caption" title="${escapeHtml(citation.title)}">${escapeHtml(citation.display)}</div>`
    : "";
  return caption + "<pre><code>" + escapeHtml(body) + "</code></pre>";
}

interface ListItemLines {
  lines: string[];
  offset: number;
}

/** Renders the list starting at `start`; returns its HTML and the first line after it. */
function renderList(lines: string[], start: number): { html: string; next: number } {
  const first = listItem(lines[start]) as RegExpExecArray;
  const baseIndent = first[1].length;
  const ordered = /\d/.test(first[2]);
  const delimiter = first[2].slice(-1);
  const items: ListItemLines[] = [];
  let loose = false;
  let sawBlank = false;
  let i = start;

  while (i < lines.length) {
    const line = lines[i];
    if (RULE.test(line) && line.length - line.trimStart().length < baseIndent + 2) break;
    const marker = listItem(line);
    const sibling = marker && marker[1].length < baseIndent + 2 ? marker : null;
    const sameKind =
      !!sibling && (ordered ? /\d/.test(sibling[2]) && sibling[2].slice(-1) === delimiter : sibling[2] === delimiter);
    // Another kind of list ends this one, unless it is "2026. That…" wrapped
    // straight onto the item, which stays the item's text.
    if (sibling && !sameKind && (sawBlank || interruptingItem(line))) break;
    if (sibling && sameKind) {
      if (sawBlank && items.length) loose = true;
      const gap = sibling[3] ?? "";
      const offset = sibling[1].length + sibling[2].length + (gap.length >= 1 && gap.length <= 4 ? gap.length : 1);
      items.push({ lines: [sibling[4] ?? ""], offset });
      sawBlank = false;
      i++;
      continue;
    }
    const current = items[items.length - 1];
    if (line.trim() === "") {
      sawBlank = true;
      current.lines.push("");
      i++;
      continue;
    }
    const indent = line.length - line.trimStart().length;
    if (indent >= Math.min(current.offset, baseIndent + 2)) {
      current.lines.push(line.slice(Math.min(indent, current.offset)));
      // A second paragraph inside an item makes the list loose.
      if (sawBlank) loose = true;
      sawBlank = false;
      i++;
      continue;
    }
    if (!sawBlank && !startsBlock(line)) {
      current.lines.push(line.trim());
      i++;
      continue;
    }
    break;
  }

  const tag = ordered ? "ol" : "ul";
  const startNumber = ordered ? parseInt(first[2], 10) : 1;
  let html = ordered && startNumber !== 1 ? `<ol start="${startNumber}">` : `<${tag}>`;
  for (const item of items) {
    while (item.lines.length && item.lines[item.lines.length - 1].trim() === "") item.lines.pop();
    let checkbox = "";
    const task = TASK.exec(item.lines[0] ?? "");
    if (task) {
      item.lines[0] = item.lines[0].slice(task[0].length);
      checkbox = `<input type="checkbox" disabled${task[1] === " " ? "" : " checked"}> `;
    }
    let body = renderBlocks(item.lines);
    if (!loose && body.startsWith("<p>")) {
      const close = body.indexOf("</p>");
      body = body.slice(3, close) + body.slice(close + 4);
    }
    html += (task ? '<li class="task">' : "<li>") + checkbox + body + "</li>";
  }
  return { html: html + `</${tag}>`, next: i };
}

function renderBlocks(lines: string[]): string {
  let html = "";
  let paragraph: string[] = [];
  const flushParagraph = () => {
    html += paragraphHtml(paragraph);
    paragraph = [];
  };

  let i = 0;
  while (i < lines.length) {
    const line = lines[i];

    const open = fenceOpen(line);
    if (open) {
      flushParagraph();
      const indent = open[1].length;
      const marker = open[2];
      const body: string[] = [];
      i++;
      while (i < lines.length) {
        const close = FENCE_CLOSE.exec(lines[i]);
        if (close && close[1][0] === marker[0] && close[1].length >= marker.length) {
          i++;
          break;
        }
        const raw = lines[i];
        const strip = Math.min(indent, raw.length - raw.trimStart().length);
        body.push(raw.slice(strip));
        i++;
      }
      html += fenceHtml(body.join("\n"), open[3]);
      continue;
    }

    if (line.trim() === "") {
      flushParagraph();
      i++;
      continue;
    }

    if (RULE.test(line)) {
      flushParagraph();
      html += "<hr>";
      i++;
      continue;
    }

    const heading = HEADING.exec(line);
    if (heading) {
      flushParagraph();
      const level = heading[1].length;
      html += `<h${level}>${inlineMarkdown(heading[2] ?? "")}</h${level}>`;
      i++;
      continue;
    }

    if (QUOTE.test(line)) {
      flushParagraph();
      const quoted: string[] = [];
      while (i < lines.length) {
        const quote = QUOTE.exec(lines[i]);
        if (quote) quoted.push(quote[1]);
        else if (lines[i].trim() && quoted.length && quoted[quoted.length - 1].trim() && !startsBlock(lines[i])) quoted.push(lines[i]);
        else break;
        i++;
      }
      html += "<blockquote>" + renderBlocks(quoted) + "</blockquote>";
      continue;
    }

    const item = paragraph.length ? interruptingItem(line) : listItem(line);
    if (item && item[3] !== undefined) {
      flushParagraph();
      const list = renderList(lines, i);
      html += list.html;
      i = list.next;
      continue;
    }

    // A separator row confirms a table, so the header and body only need a
    // pipe; that admits tables written without outer pipes (`a | b`).
    if (line.includes("|") && i + 1 < lines.length && lines[i + 1].includes("|") && isTableSeparator(lines[i + 1])) {
      flushParagraph();
      const rows = [line, lines[i + 1]];
      i += 2;
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) rows.push(lines[i++]);
      html += tableToHtml(rows);
      continue;
    }

    paragraph.push(line.trim());
    i++;
  }
  flushParagraph();
  return html;
}

export function markdownToHtml(value: unknown): string {
  const lines = String(value ?? "")
    .split(/\r?\n/)
    .map((line) => (line.startsWith("\t") ? line.replace(/^\t+/, (tabs) => "    ".repeat(tabs.length)) : line));
  return renderBlocks(lines);
}
