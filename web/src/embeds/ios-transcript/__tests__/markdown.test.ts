import { describe, expect, it } from "vitest";
import { inlineMarkdown, isTableRow, isTableSeparator, markdownToHtml, tableToHtml } from "../markdown";

describe("inlineMarkdown", () => {
  it("escapes transcript text before it styles anything", () => {
    expect(inlineMarkdown('<img src=x onerror="alert(1)"> & \'q\'')).toBe(
      "&lt;img src=x onerror=&quot;alert(1)&quot;&gt; &amp; &#39;q&#39;",
    );
  });

  it("renders links, code spans, bold and italic", () => {
    expect(inlineMarkdown("see [docs](https://example.com/a) and `x < y`, **bold** *it*")).toBe(
      'see <a href="https://example.com/a" rel="noreferrer noopener">docs</a> and <code>x &lt; y</code>, <strong>bold</strong> <em>it</em>',
    );
  });

  it("links only web and mail targets; other schemes keep their label as text", () => {
    expect(inlineMarkdown("[x](javascript:alert(1))")).toBe("x");
    expect(inlineMarkdown("[mail](mailto:a@example.com)")).toBe('<a href="mailto:a@example.com" rel="noreferrer noopener">mail</a>');
  });
});

describe("markdownToHtml", () => {
  it("renders headings, bullets, paragraphs and line breaks", () => {
    expect(markdownToHtml("# One\n## Two\n### Three\n- a\n* b\n\nline 1\nline 2\n\nnext")).toBe(
      "<h1>One</h1><h2>Two</h2><h3>Three</h3><ul><li>a</li></ul><ul><li>b</li></ul><p>line 1<br>line 2</p><p>next</p>",
    );
  });

  it("keeps fenced code verbatim and escaped", () => {
    expect(markdownToHtml("before\n```ts\nconst a = '<b>';\n| not | a table |\n```\nafter")).toBe(
      "<p>before</p><pre><code>const a = &#39;&lt;b&gt;&#39;;\n| not | a table |</code></pre><p>after</p>",
    );
  });

  it("closes an unterminated fence at the end", () => {
    expect(markdownToHtml("~~~\nopen")).toBe("<pre><code>open</code></pre>");
  });

  it("renders a GFM table with alignment", () => {
    const html = markdownToHtml("| Name | Count | Mid |\n|:--|--:|:-:|\n| a | 1 | x |\n| **b** | 2 |");
    expect(html).toBe(
      '<div class="table-wrap"><table><thead><tr><th>Name</th><th align="right">Count</th><th align="center">Mid</th></tr></thead>' +
        '<tbody><tr><td>a</td><td align="right">1</td><td align="center">x</td></tr>' +
        '<tr><td><strong>b</strong></td><td align="right">2</td><td align="center"></td></tr></tbody></table></div>',
    );
  });

  it("marks a long bold first-column cell as prose in a four-column table", () => {
    const html = markdownToHtml(
      "| Work | Status | Owner | Duration |\n|---|---|---|--:|\n| **Record today's findings in the repo** | In progress | David | 4m 12s |",
    );
    expect(html).toBe(
      '<div class="table-wrap"><table><thead><tr><th class="prose">Work</th><th>Status</th><th>Owner</th><th align="right">Duration</th></tr></thead>' +
        '<tbody><tr><td class="prose"><strong>Record today&#39;s findings in the repo</strong></td><td>In progress</td><td>David</td><td align="right">4m 12s</td></tr></tbody></table></div>',
    );
  });

  it("does not turn a lone pipe line into a table", () => {
    expect(markdownToHtml("run `ls | wc -l` now")).toBe("<p>run <code>ls | wc -l</code> now</p>");
    expect(markdownToHtml("a|b\nplain")).toBe("<p>a|b<br>plain</p>");
  });

  it("treats two pipe rows without a separator as prose", () => {
    expect(markdownToHtml("| a | b |\n| c | d |")).toBe("<p>| a | b |<br>| c | d |</p>");
  });

  it("treats a separator-only first line as prose", () => {
    expect(markdownToHtml("|---|\ntext")).toBe("<p>|---|<br>text</p>");
  });

  it("ends a table at the first non-pipe line", () => {
    expect(markdownToHtml("| h |\n|---|\n| v |\nafter")).toBe(
      '<div class="table-wrap"><table><thead><tr><th>h</th></tr></thead><tbody><tr><td>v</td></tr></tbody></table></div><p>after</p>',
    );
  });
});

describe("table helpers", () => {
  it("recognizes separators and rows", () => {
    expect(isTableSeparator("|---|:-:|")).toBe(true);
    expect(isTableSeparator("|---|")).toBe(true);
    expect(isTableSeparator("| a |")).toBe(false);
    expect(isTableRow("| a |")).toBe(true);
    expect(isTableRow("a|b")).toBe(true);
    expect(isTableRow("a | b")).toBe(false);
  });

  it("falls back to paragraphs without a real separator", () => {
    expect(tableToHtml(["| a |"])).toBe("<p>| a |</p>");
  });
});
