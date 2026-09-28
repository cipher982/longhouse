import { describe, expect, it } from "vitest";
import { collapsedPreview, stripMarkdown } from "../ReasoningRow";

describe("stripMarkdown", () => {
  it("strips bold markers, keeping the emphasized text", () => {
    expect(stripMarkdown("**Updating job commits** and pushing")).toBe(
      "Updating job commits and pushing",
    );
  });

  it("strips italics, inline code, headings, and links", () => {
    expect(stripMarkdown("_Planning_ the `deploy.sh` step")).toBe("Planning the deploy.sh step");
    expect(stripMarkdown("### Verifying deployment tasks")).toBe("Verifying deployment tasks");
    expect(stripMarkdown("See [the runbook](https://example.com/runbook) first")).toBe(
      "See the runbook first",
    );
  });

  it("leaves plain text untouched", () => {
    expect(stripMarkdown("Cross-checking the release steps")).toBe(
      "Cross-checking the release steps",
    );
  });
});

describe("collapsedPreview", () => {
  it("renders a collapsed Thinking summary with no raw markdown characters, and counts every hidden line after the first", () => {
    const preview = collapsedPreview(
      "**Planning code deployment** and checking the manual-app workflow\nCross-checking the release steps against the manual-app runbook first.\nThen push once the health check is green.",
    );
    expect(preview).not.toMatch(/[*_`#]/);
    expect(preview).toContain("Planning code deployment");
    // Two lines remain hidden behind the summary (lines 2 and 3) — never
    // joined into it.
    expect(preview).toMatch(/2 more lines$/);
  });

  it("never joins multiple lines into one run-on summary", () => {
    const preview = collapsedPreview(
      "**Updating job commits** and pushing\nCross-checking the release steps against the manual-app runbook first.",
    );
    expect(preview).toBe("Updating job commits and pushing … 1 more line");
    expect(preview).not.toContain("Cross-checking");
  });

  it("ellipsizes a long first line at about 90 characters instead of wrapping or joining", () => {
    const longFirstLine =
      "Planning code deployment and checking the manual-app workflow before doing anything else at all";
    expect(longFirstLine.length).toBeGreaterThan(90);
    const preview = collapsedPreview(longFirstLine);
    expect(preview.endsWith("…")).toBe(true);
    expect(preview.length).toBeLessThanOrEqual(91);
  });

  it("skips leading blank lines to find the first non-empty one", () => {
    const preview = collapsedPreview("\n\n**Deploying now** and watching the rollout");
    expect(preview).toBe("Deploying now and watching the rollout");
  });

  it("falls back to a placeholder for empty reasoning text", () => {
    expect(collapsedPreview("")).toBe("No reasoning details");
  });

  it("shows no hidden-line suffix for a single-line thought", () => {
    expect(collapsedPreview("**Deploying now**")).toBe("Deploying now");
  });
});
