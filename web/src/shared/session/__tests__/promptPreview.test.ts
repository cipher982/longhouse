import { describe, expect, it } from "vitest";
import { cleanPromptPreview } from "../promptPreview";

describe("cleanPromptPreview", () => {
  it("names a paste and keeps the words typed around it", () => {
    expect(
      cleanPromptPreview('Here is the draft: <pasted_content id="268e">Dear Scale…</pasted_content id="268e"> tighten it'),
    ).toBe("Here is the draft: [pasted text] tighten it");
  });

  it("shows the paste itself when nothing else was typed, without the quote fence", () => {
    expect(
      cleanPromptPreview('""" <pasted_content id="268e"> David - Staff Research on Scale\'s General Agents'),
    ).toBe("[pasted] David - Staff Research on Scale's General Agents");
  });

  it("marks images once and keeps the ask", () => {
    expect(cleanPromptPreview("[Image #1, 906×1500] [Image #2] is this just dev churn?")).toBe(
      "[image] is this just dev churn?",
    );
  });

  it("puts an image marker ahead of a paste-only prompt", () => {
    expect(
      cleanPromptPreview('[Image #1] <pasted_content id="9a2a">Let\'s do some brainstorming</pasted_content id="9a2a">'),
    ).toBe("[image] [pasted] Let's do some brainstorming");
  });

  it("unwraps attachments and drops terminal box frames", () => {
    expect(cleanPromptPreview('"""<attachment> Pairing Tapo smart plug without losing…')).toBe(
      "[attachment] Pairing Tapo smart plug without losing…",
    );
    expect(cleanPromptPreview('""" ╭────────────╮ │ week one │')).toBe("week one");
  });

  it("leaves an ordinary prompt alone apart from whitespace", () => {
    expect(cleanPromptPreview("  On my web app,\n when I click Machines  ")).toBe("On my web app, when I click Machines");
    expect(cleanPromptPreview(null)).toBe("");
  });
});
