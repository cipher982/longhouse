import { describe, expect, it } from "vitest";
import { inlineMarkdown, markdownToHtml } from "../markdown";

// What agents actually write, by provider. A local file link must read as a
// short file name, not raw markdown or a dead link.
describe("agent file references", () => {
  it("renders an Antigravity file:// link as its file name", () => {
    const html = inlineMarkdown(
      "Added it to [zero_to_one_audit.md](file:///Users/alice/.gemini/antigravity-cli/brain/0f1e2d3c-4b5a-4968-8778-a6b5c4d3e2f1/zero_to_one_audit.md) (97% fewer objects).",
    );
    expect(html).toBe(
      'Added it to <span class="file-ref" title="~/.gemini/antigravity-cli/brain/0f1e2d3c-4b5a-4968-8778-a6b5c4d3e2f1/zero_to_one_audit.md">zero_to_one_audit.md</span> (97% fewer objects).',
    );
  });

  it("appends a line range the label does not already show", () => {
    expect(inlineMarkdown("[evaluation.md](file:///home/alice/git/g/docs/evaluation.md#L59-L79)")).toBe(
      '<span class="file-ref" title="~/git/g/docs/evaluation.md:59-79">evaluation.md:59-79</span>',
    );
    expect(inlineMarkdown("[evaluation.md:59](file:///home/alice/docs/evaluation.md#L59)")).toBe(
      '<span class="file-ref" title="~/docs/evaluation.md:59">evaluation.md:59</span>',
    );
  });

  it("renders a Codex absolute path with a line suffix", () => {
    expect(inlineMarkdown("see [app.py](/Users/alice/repo/app.py:12)")).toBe(
      'see <span class="file-ref" title="~/repo/app.py:12">app.py:12</span>',
    );
    expect(inlineMarkdown("[/Users/alice/repo/app.py:12:3](/Users/alice/repo/app.py:12:3)")).toBe(
      '<span class="file-ref" title="~/repo/app.py:12">app.py:12</span>',
    );
  });

  it("keeps a relative path as written", () => {
    expect(inlineMarkdown("[docs/README.md](docs/README.md)")).toBe(
      '<span class="file-ref" title="docs/README.md">docs/README.md</span>',
    );
  });

  it("drops an in-page anchor to its label", () => {
    expect(inlineMarkdown("[checks](#6-checks)")).toBe("checks");
  });

  it("captions a Cursor code citation fence", () => {
    expect(markdownToHtml("```12:15:app/components/Todo.tsx\nconst a = 1;\n```")).toBe(
      '<div class="code-caption" title="app/components/Todo.tsx:12-15">Todo.tsx:12-15</div><pre><code>const a = 1;</code></pre>',
    );
  });

  it("shortens a home path in a code span and keeps the full one as its title", () => {
    expect(inlineMarkdown("`/Users/alice/git/x/y.ts`")).toBe('<code title="/Users/alice/git/x/y.ts">~/git/x/y.ts</code>');
    expect(inlineMarkdown("`cd /Users/alice`")).toBe("<code>cd /Users/alice</code>");
  });
});

describe("inline markdown", () => {
  it("never styles inside a code span", () => {
    expect(inlineMarkdown("`a*b*c` and `**x**` and `[l](https://x.io)`")).toBe(
      "<code>a*b*c</code> and <code>**x**</code> and <code>[l](https://x.io)</code>",
    );
  });

  it("handles double-backtick spans holding a backtick", () => {
    expect(inlineMarkdown("`` a`b ``")).toBe("<code>a`b</code>");
    expect(inlineMarkdown("unclosed ` tick")).toBe("unclosed ` tick");
  });

  it("does not italicize arithmetic or snake_case", () => {
    expect(inlineMarkdown("2 * 3 * 4 and my_var_name")).toBe("2 * 3 * 4 and my_var_name");
  });

  it("renders bold italic, underscore emphasis and strikethrough", () => {
    expect(inlineMarkdown("***both*** __bold__ _it_ ~~gone~~")).toBe(
      "<strong><em>both</em></strong> <strong>bold</strong> <em>it</em> <del>gone</del>",
    );
  });

  it("honors backslash escapes", () => {
    expect(inlineMarkdown("\\*not em\\* and \\[x\\](y)")).toBe("*not em* and [x](y)");
  });

  it("links bare and angle-bracket URLs without swallowing punctuation", () => {
    expect(inlineMarkdown("see https://example.com/a_b. Then <https://x.io/q>")).toBe(
      'see <a href="https://example.com/a_b" rel="noreferrer noopener">https://example.com/a_b</a>. Then <a href="https://x.io/q" rel="noreferrer noopener">https://x.io/q</a>',
    );
    expect(inlineMarkdown("(https://en.wikipedia.org/wiki/Foo_(bar))")).toBe(
      '(<a href="https://en.wikipedia.org/wiki/Foo_(bar)" rel="noreferrer noopener">https://en.wikipedia.org/wiki/Foo_(bar)</a>)',
    );
  });

  it("keeps parens in a link target and styles its label", () => {
    expect(inlineMarkdown("[**Foo**](https://en.wikipedia.org/wiki/Foo_(bar))")).toBe(
      '<a href="https://en.wikipedia.org/wiki/Foo_(bar)" rel="noreferrer noopener"><strong>Foo</strong></a>',
    );
  });

  it("escapes markup in link labels and targets", () => {
    expect(inlineMarkdown('[<b>x</b>](https://x.io/"onmouseover=1)')).toBe(
      '<a href="https://x.io/&quot;onmouseover=1" rel="noreferrer noopener">&lt;b&gt;x&lt;/b&gt;</a>',
    );
  });
});

describe("block markdown", () => {
  it("renders an ordered list with a start number", () => {
    expect(markdownToHtml("Steps:\n1. one\n2. two")).toBe("<p>Steps:</p><ol><li>one</li><li>two</li></ol>");
    expect(markdownToHtml("3. three\n4. four")).toBe('<ol start="3"><li>three</li><li>four</li></ol>');
  });

  it("keeps a wrapped sentence starting with a year as prose", () => {
    expect(markdownToHtml("It shipped in\n2026. Then")).toBe("<p>It shipped in<br>2026. Then</p>");
  });

  it("nests lists by indentation, including two-space bullets under a numbered item", () => {
    expect(markdownToHtml("1. Data\n  - camera\n  - route\n2. Model")).toBe(
      "<ol><li>Data<ul><li>camera</li><li>route</li></ul></li><li>Model</li></ol>",
    );
  });

  it("keeps wrapped item text and fenced code inside the item", () => {
    expect(markdownToHtml("- first line\n  wraps here\n- run:\n  ```sh\n  make test\n  ```")).toBe(
      "<ul><li>first line<br>wraps here</li><li>run:<pre><code>make test</code></pre></li></ul>",
    );
  });

  it("makes a list with blank lines between items loose", () => {
    expect(markdownToHtml("- a\n\n- b")).toBe("<ul><li><p>a</p></li><li><p>b</p></li></ul>");
  });

  it("ends a list at an unindented line after a blank", () => {
    expect(markdownToHtml("- a\n\nafter")).toBe("<ul><li>a</li></ul><p>after</p>");
  });

  it("renders task items", () => {
    expect(markdownToHtml("- [x] done\n- [ ] todo")).toBe(
      '<ul><li class="task"><input type="checkbox" disabled checked> done</li><li class="task"><input type="checkbox" disabled> todo</li></ul>',
    );
  });

  it("renders block quotes, rules and h4-h6", () => {
    expect(markdownToHtml("> **Note:** quoted\n> more\n\n---\n#### Four\n###### Six ##")).toBe(
      "<blockquote><p><strong>Note:</strong> quoted<br>more</p></blockquote><hr><h4>Four</h4><h6>Six</h6>",
    );
  });

  it("does not read bold at line start as a bullet", () => {
    expect(markdownToHtml("**Operational state:** idle")).toBe("<p><strong>Operational state:</strong> idle</p>");
  });

  it("lets a longer fence hold a shorter one", () => {
    expect(markdownToHtml("````md\n```js\nx\n```\n````")).toBe("<pre><code>```js\nx\n```</code></pre>");
  });

  it("treats triple backticks with text on one line as a code span", () => {
    expect(markdownToHtml("use ```x``` here")).toBe("<p>use <code>x</code> here</p>");
  });
});
