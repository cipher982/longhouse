import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MarkdownBody } from "../MarkdownBody";
import { resolveHighlightLanguage } from "../syntaxHighlighter";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("resolveHighlightLanguage", () => {
  it("resolves supported languages and aliases case-insensitively", () => {
    expect(resolveHighlightLanguage("rust")).toBe("rust");
    expect(resolveHighlightLanguage("RS")).toBe("rs");
    expect(resolveHighlightLanguage("golang")).toBe("golang");
    expect(resolveHighlightLanguage("toml")).toBe("toml");
    expect(resolveHighlightLanguage("diff")).toBe("diff");
    expect(resolveHighlightLanguage("zsh")).toBe("zsh");
  });

  it("returns null for unknown or empty languages", () => {
    expect(resolveHighlightLanguage(null)).toBeNull();
    expect(resolveHighlightLanguage("")).toBeNull();
    expect(resolveHighlightLanguage("brainfuck")).toBeNull();
  });
});

describe("MarkdownBody", () => {
  it("renders headings, tables, inline code, and code inside links", () => {
    const md = [
      "### Architecture",
      "",
      "Use `tokio` with [`EventRecord`](https://example.com/er).",
      "",
      "| Component | Crate |",
      "| --- | --- |",
      "| **Server** | `axum` |",
    ].join("\n");

    const { container } = render(<MarkdownBody content={md} />);
    expect(container.querySelector("h3")?.textContent).toBe("Architecture");
    expect(container.querySelector("thead th")?.textContent).toBe("Component");
    expect(container.querySelector("a code")?.textContent).toBe("EventRecord");
    expect(container.querySelector("strong")?.textContent).toBe("Server");
  });

  it("highlights supported fenced blocks and copies without trailing newline", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });

    const md = "```rust\nfn main() {\n    println!(\"hi\");\n}\n```";
    const { container } = render(<MarkdownBody content={md} />);

    expect(container.querySelector(".tl-md-fence__lang")?.textContent).toBe("rust");
    // Highlighted block renders via SyntaxHighlighter (not the plain fallback class)
    expect(container.querySelector(".tl-md-fence__plain")).toBeNull();

    const copyBtn = screen.getByRole("button", { name: "Copy code" });
    fireEvent.click(copyBtn);

    await waitFor(() => {
      expect(writeText).toHaveBeenCalledWith('fn main() {\n    println!("hi");\n}');
      expect(copyBtn.textContent).toBe("Copied");
    });
  });

  it("falls back to plain pre for no-language, unknown-language, and >400-line fences", () => {
    const noLang = "```\nplain text\n```";
    const unknownLang = "```obscurelang\nfoo bar\n```";
    const hugeFence = "```rust\n" + Array.from({ length: 405 }, (_, i) => `let x${i} = ${i};`).join("\n") + "\n```";

    const { container: c1 } = render(<MarkdownBody content={noLang} />);
    expect(c1.querySelector(".tl-md-fence__lang")?.textContent).toBe("text");
    expect(c1.querySelector(".tl-md-fence__plain")).not.toBeNull();

    const { container: c2 } = render(<MarkdownBody content={unknownLang} />);
    expect(c2.querySelector(".tl-md-fence__lang")?.textContent).toBe("obscurelang");
    expect(c2.querySelector(".tl-md-fence__plain")).not.toBeNull();

    const { container: c3 } = render(<MarkdownBody content={hugeFence} />);
    expect(c3.querySelector(".tl-md-fence__lang")?.textContent).toBe("rust");
    expect(c3.querySelector(".tl-md-fence__plain")).not.toBeNull();
  });

  it("copies rendered markdown tables as clean GFM", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });

    const md = [
      "| Phase | Time |",
      "| --- | --- |",
      "| **Train** | `4m` |",
    ].join("\n");

    render(<MarkdownBody content={md} />);
    const copyBtn = screen.getByRole("button", { name: "Copy table" });
    fireEvent.click(copyBtn);

    await waitFor(() => {
      expect(writeText).toHaveBeenCalledWith(
        "| Phase | Time |\n| --- | --- |\n| Train | 4m |",
      );
      expect(copyBtn.textContent).toBe("Copied");
    });
  });
});
