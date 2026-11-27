import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { MarkdownBody } from "../MarkdownBody";

afterEach(cleanup);

describe("MarkdownBody link targets", () => {
  it("renders an agent's file:// link as a file name, not an empty link", () => {
    const { container } = render(
      <MarkdownBody content="Wrote [plan.md](file:///Users/alice/.gemini/brain/0f1e2d3c-4b5a/plan.md#L3-L9) today." />,
    );
    expect(container.querySelector("a")).toBeNull();
    const ref = container.querySelector(".tl-file-ref");
    expect(ref?.textContent).toBe("plan.md:3-9");
    expect(ref?.getAttribute("title")).toBe("~/.gemini/brain/0f1e2d3c-4b5a/plan.md:3-9");
  });

  it("renders Codex and relative paths as file refs", () => {
    const { container } = render(<MarkdownBody content="[app.py](/home/alice/r/app.py:12) and [docs/a.md](docs/a.md)" />);
    const refs = [...container.querySelectorAll(".tl-file-ref")].map((el) => el.textContent);
    expect(refs).toEqual(["app.py:12", "docs/a.md"]);
    expect(container.querySelector("a")).toBeNull();
  });

  it("keeps web links as new-tab links", () => {
    const { container } = render(<MarkdownBody content="[docs](https://example.com/a) and https://x.io" />);
    const links = [...container.querySelectorAll("a")];
    expect(links.map((a) => a.getAttribute("href"))).toEqual(["https://example.com/a", "https://x.io"]);
    expect(links.every((a) => a.getAttribute("target") === "_blank")).toBe(true);
  });

  it("never emits a script or anchor href", () => {
    const { container } = render(<MarkdownBody content="[x](javascript:alert(1)) [y](#section)" />);
    expect(container.querySelector("a")).toBeNull();
    expect(container.textContent).toBe("x y");
  });

  it("shortens a home path in inline code", () => {
    const { container } = render(<MarkdownBody content="edit `/Users/alice/git/x.ts` now" />);
    const code = container.querySelector("code");
    expect(code?.textContent).toBe("~/git/x.ts");
    expect(code?.getAttribute("title")).toBe("/Users/alice/git/x.ts");
  });

  it("labels a Cursor code citation fence with the file and lines", () => {
    const { container } = render(<MarkdownBody content={"```12:15:app/components/Todo.tsx\nconst a = 1;\n```"} />);
    const label = container.querySelector(".tl-md-fence__lang");
    expect(label?.textContent).toBe("Todo.tsx:12-15");
    expect(label?.getAttribute("title")).toBe("app/components/Todo.tsx:12-15");
  });
});
