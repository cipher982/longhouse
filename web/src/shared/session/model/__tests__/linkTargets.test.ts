import { describe, expect, it } from "vitest";
import { classifyLinkTarget, parseCodeCitation, parseFileTarget, shortenHomeCodePath } from "../linkTargets";

describe("parseFileTarget", () => {
  it("reads file URLs, absolute, relative and Windows paths with line ranges", () => {
    expect(parseFileTarget("file:///Users/a/x.py#L5-L9")).toEqual({ path: "/Users/a/x.py", lines: "5-9" });
    expect(parseFileTarget("file://localhost/tmp/my%20file.md")).toEqual({ path: "/tmp/my file.md", lines: null });
    expect(parseFileTarget("/repo/app.py:12:3")).toEqual({ path: "/repo/app.py", lines: "12" });
    expect(parseFileTarget("src/a.ts:10-20")).toEqual({ path: "src/a.ts", lines: "10-20" });
    expect(parseFileTarget("src/a.ts#L7")).toEqual({ path: "src/a.ts", lines: "7" });
    expect(parseFileTarget("C:\\repo\\a.ts")).toEqual({ path: "C:\\repo\\a.ts", lines: null });
  });

  it("rejects anchors and other schemes", () => {
    for (const raw of ["#top", "javascript:alert(1)", "app://x", "data:text/html,x", "", "file:///"]) {
      expect(parseFileTarget(raw)).toBeNull();
    }
  });
});

describe("classifyLinkTarget", () => {
  it("keeps web and mail links", () => {
    expect(classifyLinkTarget("https://x.io", "x")).toEqual({ kind: "web", href: "https://x.io" });
    expect(classifyLinkTarget("mailto:a@b.c", "mail")).toEqual({ kind: "web", href: "mailto:a@b.c" });
  });

  it("names an absolute file by its basename and keeps a descriptive label", () => {
    expect(classifyLinkTarget("file:///home/a/r/notes.md", "file:///home/a/r/notes.md")).toEqual({
      kind: "file",
      display: "notes.md",
      title: "~/r/notes.md",
    });
    expect(classifyLinkTarget("/home/a/r/notes.md#L4", "the plan")).toEqual({
      kind: "file",
      display: "the plan:4",
      title: "~/r/notes.md:4",
    });
  });
});

describe("helpers", () => {
  it("shortens only a whole home path", () => {
    expect(shortenHomeCodePath("/Users/a/git/x")).toBe("~/git/x");
    expect(shortenHomeCodePath("/opt/x")).toBeNull();
    expect(shortenHomeCodePath("ls /Users/a")).toBeNull();
  });

  it("parses Cursor code citations", () => {
    expect(parseCodeCitation("3:3:src/a.ts")).toEqual({ path: "src/a.ts", display: "a.ts:3", title: "src/a.ts:3" });
    expect(parseCodeCitation("typescript")).toBeNull();
  });
});
