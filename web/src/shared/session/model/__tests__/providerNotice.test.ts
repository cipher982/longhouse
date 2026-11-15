import { describe, expect, it } from "vitest";
import {
  summarizeApiError,
  summarizeCompactionNotice,
  summarizeProviderNotice,
  summarizeTaskNotification,
} from "../providerNotice";

// Served text: what `provider_display_message_text` and Claude's task
// notification summary put in `content_text` of a provider_notification row.
const OMP_SHORT = "Background job bg_1 has completed.\nSMOKE-STEP-1\nSMOKE-STEP-2\nSMOKE-STEP-3\nWall time: 180.02 seconds";
const OMP_ELIDED = [
  "Background job bg_96 has completed.",
  "…",
  "ered job: zerg-tenant-data-reserve (cron=*/5 * * * *, enabled=True)",
  "2026-09-30 15:24:21,974 [INFO] sauron.jobs.registry: Registered job: llm-bench-discovery (cron=0 7 * * *, enabled=False)",
  "[Output truncated. Showing first 4,000 characters.]",
  "Full output: artifact://335",
].join("\n");

describe("summarizeProviderNotice", () => {
  it("collapses an OMP job result to its header, first output line and line count", () => {
    expect(summarizeProviderNotice(OMP_SHORT)).toEqual({
      title: "Background job bg_1 has completed",
      hint: "SMOKE-STEP-1 … 3 more lines",
      body: "SMOKE-STEP-1\nSMOKE-STEP-2\nSMOKE-STEP-3\nWall time: 180.02 seconds",
    });
  });

  it("does not preview the mid-line fragment after the server's elision marker", () => {
    const summary = summarizeProviderNotice(OMP_ELIDED);
    expect(summary.title).toBe("Background job bg_96 has completed");
    expect(summary.hint).toMatch(/^2026-09-30 15:24:21,974 \[INFO\] sauron\.jobs\.registry: Registered job: llm-bench-discovery/);
    expect(summary.hint).toMatch(/… 2 more lines$/);
    // Expanding shows everything the server kept, marker included.
    expect(summary.body).toBe(OMP_ELIDED.split("\n").slice(1).join("\n"));
  });

  it("leaves a header-only notice with nothing to open", () => {
    expect(summarizeProviderNotice('Background command "Run the checks" completed (exit code 0)')).toEqual({
      title: 'Background command "Run the checks" completed (exit code 0)',
      hint: null,
      body: null,
    });
  });

  it("treats a header followed only by the elision marker as header-only", () => {
    expect(summarizeProviderNotice("Background job bg_2 has completed.\n…\n").body).toBeNull();
  });

  it("says one more line in the singular and shows a lone output line without a count", () => {
    expect(summarizeProviderNotice("Job done.\nfirst\nsecond").hint).toBe("first … 1 more line");
    expect(summarizeProviderNotice("Job done.\nonly line").hint).toBe("only line");
  });

  it("skips blank lines and CRLF when choosing the hint, but keeps the output verbatim", () => {
    const summary = summarizeProviderNotice("Job done.\r\n\r\nfirst\r\n\r\nsecond\r\n");
    expect(summary.hint).toBe("first … 1 more line");
    expect(summary.body).toBe("first\n\nsecond");
  });

  it("ellipsizes a long first output line", () => {
    const summary = summarizeProviderNotice(`Job done.\n${"x".repeat(200)}`);
    expect(summary.hint).toBe(`${"x".repeat(90)}…`);
    expect(summary.body).toBe("x".repeat(200));
  });

  it("makes an over-long header expandable to the whole text", () => {
    const header = `Background job ${"very-long-name-".repeat(12)} finished`;
    const summary = summarizeProviderNotice(`${header}\nmore`);
    expect(summary.title.length).toBeLessThanOrEqual(121);
    expect(summary.title.endsWith("…")).toBe(true);
    expect(summary.hint).toBeNull();
    expect(summary.body).toBe(`${header}\nmore`);
  });

  it("falls back to a generic title for an empty notice", () => {
    for (const empty of [null, undefined, "", " \n "]) {
      expect(summarizeProviderNotice(empty)).toEqual({ title: "Provider update", hint: null, body: null });
    }
  });
});

describe("summarizeCompactionNotice", () => {
  it("formats Claude compact metadata hints concisely", () => {
    expect(summarizeCompactionNotice("Conversation compacted [trigger=auto pre_tokens=155708]")).toEqual({
      title: "Context compacted",
      detail: "Auto · 155.7k tokens",
    });
  });

  it("handles bare default compaction text without extra chrome", () => {
    expect(summarizeCompactionNotice("Conversation compacted")).toEqual({
      title: "Context compacted",
      detail: null,
    });
    expect(summarizeCompactionNotice(null)).toEqual({
      title: "Context compacted",
      detail: null,
    });
  });

  it("preserves provider-authored compaction summary text", () => {
    expect(summarizeCompactionNotice("Earlier work focused on parser migration.\nSecond line.")).toEqual({
      title: "Context compacted",
      detail: "Earlier work focused on parser migration.",
    });
  });

  it("formats Claude microcompact boundaries", () => {
    expect(summarizeCompactionNotice("Context microcompacted [trigger=auto tokens_saved=14200]")).toEqual({
      title: "Context microcompacted",
      detail: "Auto · 14.2k saved",
    });
    expect(summarizeCompactionNotice("Context microcompacted")).toEqual({
      title: "Context microcompacted",
      detail: null,
    });
  });
});

describe("summarizeTaskNotification", () => {
  it("formats duration, tokens, tool uses, note, and result", () => {
    const summary = summarizeTaskNotification({
      task_id: "a123",
      status: "completed",
      summary: "Agent \"Inspect hooks\" completed",
      duration_ms: 45_000,
      subagent_tokens: 12_400,
      tool_uses: 7,
      note: "Merged into working tree",
      result: "All hook paths verified.",
    });
    expect(summary.title).toBe('Agent "Inspect hooks" completed');
    expect(summary.meta).toBe("45s · 12.4k tokens · 7 tool uses");
    expect(summary.note).toBe("Merged into working tree");
    expect(summary.result).toBe("All hook paths verified.");
  });

  it("handles absorbed additional_items in one user notification record", () => {
    const summary = summarizeTaskNotification({
      task_id: "t1",
      status: "completed",
      summary: "First done",
      additional_items: [{ task_id: "t2", status: "failed", summary: "Second failed", duration_ms: 5_000 }],
    });
    expect(summary.title).toBe("2 background tasks updated");
    expect(summary.entries).toHaveLength(2);
    expect(summary.entries[1]?.summary).toBe("Second failed");
  });
});

describe("summarizeApiError", () => {
  it("extracts retry badge and details", () => {
    const summary = summarizeApiError({
      retry_attempt: 2,
      max_retries: 4,
      status_code: 529,
      retry_in_ms: 4000,
      error: "Overloaded",
      details: "upstream saturated",
    });
    expect(summary.badge).toBe("retry 2/4 · HTTP 529 · in 4s");
    expect(summary.title).toBe("Overloaded");
    expect(summary.detail).toBe("upstream saturated");
  });
});
