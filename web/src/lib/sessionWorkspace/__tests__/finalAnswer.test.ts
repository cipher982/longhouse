import { describe, expect, it } from "vitest";
import { formatFinalAnswer } from "../finalAnswer";
import { isFinalAnswerTool } from "../toolTiers.generated";

describe("isFinalAnswerTool", () => {
  it("recognizes the schema-constrained return tool, case-insensitively", () => {
    expect(isFinalAnswerTool("StructuredOutput")).toBe(true);
    expect(isFinalAnswerTool("structuredoutput")).toBe(true);
  });

  it("leaves ordinary tools alone", () => {
    expect(isFinalAnswerTool("Bash")).toBe(false);
    expect(isFinalAnswerTool("Write")).toBe(false);
  });
});

describe("formatFinalAnswer", () => {
  it("returns a lone string field as the whole answer, without its key", () => {
    expect(formatFinalAnswer({ report: "Everything landed." })).toBe("Everything landed.");
  });

  it("renders each field with sorted keys and bulleted string lists", () => {
    expect(
      formatFinalAnswer({
        done: ["Dropped capabilities.", "Documented residual risk."],
        changed: ["Dockerfile"],
      }),
    ).toBe("**changed**\n\n- Dockerfile\n\n**done**\n\n- Dropped capabilities.\n- Documented residual risk.");
  });

  it("renders scalars inline", () => {
    expect(formatFinalAnswer({ confident: true, findings: 3 })).toBe(
      "**confident**\n\ntrue\n\n**findings**\n\n3",
    );
  });

  it("falls back to key-sorted fenced JSON for nested values", () => {
    expect(formatFinalAnswer({ verdict: "real", evidence: { line: 12, file: "a.py" } })).toBe(
      '**evidence**\n\n```json\n{\n  "file": "a.py",\n  "line": 12\n}\n```\n\n**verdict**\n\nreal',
    );
  });

  it("skips null and empty fields rather than printing placeholders", () => {
    expect(formatFinalAnswer({ notes: null, skipped: [], summary: "Done.", ok: true })).toBe(
      "**ok**\n\ntrue\n\n**summary**\n\nDone.",
    );
  });

  it("parses a JSON string payload, and keeps a non-JSON string verbatim", () => {
    expect(formatFinalAnswer('{"answer": "42"}')).toBe("42");
    expect(formatFinalAnswer("just prose")).toBe("just prose");
  });

  it("returns null when there is nothing to show, so the tool row survives", () => {
    expect(formatFinalAnswer(null)).toBeNull();
    expect(formatFinalAnswer({})).toBeNull();
    expect(formatFinalAnswer("")).toBeNull();
    expect(formatFinalAnswer({ summary: "   " })).toBeNull();
  });
});
