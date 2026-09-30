import { readFileSync } from "node:fs";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderTranscript, resetTranscriptStateForTesting, waitForTranscriptFrame } from "../render";
import { setStickToBottom } from "../scroll";
import type { TranscriptItem, TranscriptPayload } from "../types";

// The golden payload Swift's TranscriptPayloadGoldenTests pins: what
// TimelineBuilder + WebTranscriptView.payloadItems produce for the hostile
// transcript fixture. Rendering it here closes the loop from projection to DOM.
const goldenItems: TranscriptItem[] = JSON.parse(
  readFileSync(
    path.resolve(import.meta.dirname, "../../../../../tests/fixtures/transcript-payload/hostile-transcript.golden.json"),
    "utf8",
  ),
);

function encode(payload: TranscriptPayload): string {
  // Swift sends base64 of the UTF-8 JSON.
  const bytes = new TextEncoder().encode(JSON.stringify(payload));
  return btoa(Array.from(bytes, (byte) => String.fromCharCode(byte)).join(""));
}

let sequence = 0;
function render(payload: TranscriptPayload, options: { stick?: boolean; mode?: string } = {}) {
  sequence += 1;
  return renderTranscript(encode(payload), options.stick ?? true, sequence, options.mode ?? "html");
}

const root = () => document.getElementById("root")!;
const rows = () => Array.from(root().children);

// jsdom has no layout, so scrolling is recorded rather than performed.
let scrollTo: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  document.body.innerHTML = '<main id="root" aria-live="polite"></main>';
  resetTranscriptStateForTesting();
  scrollTo = vi.spyOn(window, "scrollTo").mockImplementation(() => {});
});

afterEach(() => {
  scrollTo.mockRestore();
  delete (window as unknown as { webkit?: unknown }).webkit;
});

describe("renderTranscript against the golden payload", () => {
  it("renders every golden item with its semantics", async () => {
    const metrics = render({ errorMessage: null, items: goldenItems });
    expect(Object.keys(metrics).sort()).toEqual(["decode_ms", "dom_ms", "html_ms"]);

    const user = root().querySelector(".row.message.user .bubble");
    expect(user?.textContent).toBe("Investigate the failing job and report what Alex changed.");

    const prose = root().querySelector(".row.message.assistant .message-content")!;
    expect(prose.innerHTML).toBe(
      "<p>Now I can see what Alex did. Two blockers appeared: <strong>PROJ-101</strong> and <strong>PROJ-102</strong>. " +
        'Here is a <code>code</code> span and a <a href="https://example.com" rel="noreferrer noopener">link</a>.</p>',
    );

    const group = root().querySelector("details.passive.row")!;
    expect(group.querySelector(".tool-title")?.textContent).toBe("Searched 2 searches, read 1 file, ran 1 command");
    expect(group.querySelectorAll(".passive-call")).toHaveLength(4);
    expect(group.querySelector(".passive-earlier-btn")).toBeNull();

    const orphan = root().querySelector("details.tool.row")!;
    expect(orphan.querySelector(".tool-title")?.textContent).toBe("getJiraIssue");
    const meta = orphan.querySelector(".tool-meta")!;
    expect(meta.className).toBe("tool-meta orphan");
    expect(meta.textContent).toBe("0ms");

    await expect(waitForTranscriptFrame(sequence)).resolves.toMatchObject({
      raf_ms: expect.any(Number),
      total_ms: expect.any(Number),
    });
    await expect(waitForTranscriptFrame(sequence)).rejects.toThrow(`missing transcript frame ${sequence}`);
  });

  it("renders the same DOM in retained mode", () => {
    render({ items: goldenItems });
    const html = root().innerHTML;
    resetTranscriptStateForTesting();
    document.body.innerHTML = '<main id="root"></main>';
    render({ items: goldenItems }, { mode: "retained" });
    // Retained keeps each item's first root element; the golden has no
    // sibling preview or worker list, so the text content matches exactly.
    expect(root().textContent?.replace(/\s+/g, " ")).toBe(
      new DOMParser().parseFromString(html, "text/html").body.textContent?.replace(/\s+/g, " "),
    );
  });
});

describe("empty and error documents", () => {
  it("shows the empty state and a lone error", () => {
    render({ items: [] });
    expect(root().innerHTML).toBe('<div class="empty">No messages yet</div>');
    render({ errorMessage: "Load <failed>", items: [] });
    expect(root().innerHTML).toBe('<div class="error">Load &lt;failed&gt;</div>');
  });

  it("puts an error row above existing items", () => {
    render({ errorMessage: "Refresh failed", items: goldenItems.slice(0, 1) });
    expect(rows()[0].outerHTML).toBe('<div class="error row">Refresh failed</div>');
    expect(rows()).toHaveLength(2);
  });
});

describe("retained mode", () => {
  const item = (id: string, body: string, extra: Partial<TranscriptItem> = {}): TranscriptItem => ({
    id,
    kind: "message",
    role: "assistant",
    body,
    calls: [],
    collapsed: false,
    ...extra,
  });

  it("reuses a node while its item is unchanged and rebuilds it when it changes", () => {
    render({ items: [item("a", "first"), item("b", "second")] }, { mode: "retained" });
    const [a, b] = rows();

    render({ items: [item("a", "first"), item("b", "second, edited"), item("c", "third")] }, { mode: "retained" });
    const [a2, b2, c2] = rows();
    expect(a2).toBe(a);
    expect(b2).not.toBe(b);
    expect(b2.textContent).toContain("second, edited");
    expect(c2.textContent).toContain("third");
  });

  it("re-indexes a reused node when history is prepended", () => {
    render({ items: [item("b", "later", { collapsed: true, fullBody: "later, in full" })] }, { mode: "retained" });
    const reused = rows()[0];
    expect(reused.getAttribute("data-message-index")).toBe("0");

    render({ items: [item("a", "earlier"), item("b", "later", { collapsed: true, fullBody: "later, in full" })] }, { mode: "retained" });
    expect(rows()[1]).toBe(reused);
    expect(reused.getAttribute("data-message-index")).toBe("1");
    expect(reused.querySelector("[data-expand-index]")?.getAttribute("data-expand-index")).toBe("1");

    // The expand handler follows the new index to the right item.
    (reused.querySelector(".expand") as HTMLButtonElement).click();
    expect(reused.querySelector(".message-content")?.innerHTML).toBe("<p>later, in full</p>");
    expect(reused.querySelector(".expand")).toBeNull();
  });

  it("drops nodes for items that left the payload", () => {
    render({ items: [item("a", "one"), item("b", "two")] }, { mode: "retained" });
    render({ items: [item("b", "two")] }, { mode: "retained" });
    expect(rows()).toHaveLength(1);
    expect(rows()[0].textContent).toContain("two");
  });
});

describe("rows", () => {
  it("expands a collapsed user message to its full text", () => {
    render({ items: [{ id: "u", kind: "message", role: "user", body: "short…", fullBody: "<the whole thing>", collapsed: true }] });
    (root().querySelector(".expand") as HTMLButtonElement).click();
    expect(root().querySelector(".bubble")?.textContent).toBe("<the whole thing>");
  });

  describe("provider notifications", () => {
    const notice = (id: string, body: string): TranscriptItem => ({ id, kind: "providerNotification", body });
    const jobResult = notice(
      "provider-notification:3",
      "Background job bg_96 has completed.\nSMOKE-STEP-1\nSMOKE-STEP-2\n[Output truncated.]\nFull output: artifact://335",
    );

    it("renders a job result as a collapsed tool-shaped row with a hint and its output behind the tap", () => {
      render({ items: [jobResult] });

      const row = root().querySelector("details.tool.notice") as HTMLDetailsElement;
      expect(row.getAttribute("data-testid")).toBe("session-provider-notification");
      expect(row.open).toBe(false);
      expect(row.querySelector(".tool-title")?.textContent).toBe("Background job bg_96 has completed");
      expect(row.querySelector(".tool-subtitle")?.textContent).toBe("SMOKE-STEP-1 … 3 more lines");
      expect(row.querySelector(".details-body pre code")?.textContent).toBe(
        "SMOKE-STEP-1\nSMOKE-STEP-2\n[Output truncated.]\nFull output: artifact://335",
      );
    });

    it("renders a header-only notice as a plain row with no disclosure", () => {
      render({ items: [notice("provider-notification:4", 'Background command "Run the checks" completed (exit code 0)')] });

      expect(root().querySelector("details")).toBeNull();
      const row = root().querySelector("div.tool.notice.static")!;
      expect(row.querySelector(".tool-title")?.textContent).toBe('Background command "Run the checks" completed (exit code 0)');
    });

    it("escapes the notice text", () => {
      render({ items: [notice("provider-notification:5", "Job <b>done</b>.\n<script>alert(1)</script>")] });

      expect(root().querySelector("script")).toBeNull();
      expect(root().querySelector(".tool-title")?.textContent).toBe("Job <b>done</b>");
    });

    it("keeps an opened notice open when a later render rebuilds the transcript", () => {
      render({ items: [jobResult] });
      (root().querySelector("details.notice") as HTMLDetailsElement).open = true;

      render({ items: [jobResult, { id: "a", kind: "message", role: "assistant", body: "next" }] });
      expect((root().querySelector("details.notice") as HTMLDetailsElement).open).toBe(true);
    });
  });

  it("marks a Longhouse-sent message", () => {
    render({ items: [{ id: "u", kind: "message", role: "user", body: "hi", origin: "longhouse" }] });
    expect(root().querySelector("#session-chat-input-origin-longhouse")?.getAttribute("aria-label")).toBe("Sent via Longhouse");
  });

  it("puts the turn footer inside the item's root", () => {
    render({ items: [{ id: "p", kind: "message", role: "assistant", body: "done", turnEnd: { label: "Worked for 2m 9s", doneAt: "Turn finished 9:15 AM" } }] });
    expect(rows()).toHaveLength(1);
    expect(rows()[0].querySelector('[data-testid="session-turn-end"]')?.textContent).toBe(
      "✻ Worked for 2m 9s · Turn finished 9:15 AM",
    );
  });

  it("shows a failure preview and exit chip instead of a duration", () => {
    render({
      items: [{ id: "t", kind: "tool", title: "Bash", status: "exit 2", duration: "3s", failurePreview: "boom", calls: [] }],
    });
    expect(root().querySelector(".tool-meta")?.className).toBe("tool-meta failed");
    expect(root().querySelector(".tool-meta")?.textContent).toBe("exit 2");
    expect(root().querySelector(".failure-preview pre")?.textContent).toBe("boom");
  });

  it("replaces the input block with a diff", () => {
    render({
      items: [
        {
          id: "e",
          kind: "tool",
          title: "Edit",
          input: "raw",
          diff: [
            { kind: "remove", text: "old" },
            { kind: "add", text: "new" },
            { kind: "equal", text: "" },
          ],
        },
      ],
    });
    const lines = Array.from(root().querySelectorAll(".diff-line")).map((line) => [line.className, line.textContent]);
    expect(lines).toEqual([
      ["diff-line diff-line--remove", "−old"],
      ["diff-line diff-line--add", "+new"],
      ["diff-line diff-line--equal", "  "],
    ]);
    expect(root().textContent).not.toContain("raw");
  });

  it("folds all but the last eight calls of an activity group behind a button", () => {
    const calls = Array.from({ length: 10 }, (_, i) => ({ title: `call ${i}`, subtitle: "", status: "done" }));
    render({ items: [{ id: "g", kind: "activityGroup", title: "Ran 10 commands", calls }] });
    expect(root().querySelector(".passive-earlier-btn")?.textContent).toBe("Show 2 earlier");
    expect(root().querySelectorAll(".passive-earlier .passive-call")).toHaveLength(2);
  });

  it("renders media, pending placeholders and animated stills", () => {
    render({
      items: [
        {
          id: "m",
          kind: "message",
          role: "assistant",
          body: "",
          media: [
            { sha256: "abcdef0123456789", url: "/media/a", blobUrl: "/blob/a", mimeType: "image/gif", width: 10, height: 20 },
            { sha256: "x", url: null, mediaState: "pending" },
          ],
        },
      ],
    });
    const link = root().querySelector("a.media-item")!;
    expect(link.getAttribute("href")).toBe("/blob/a");
    const img = link.querySelector("img")!;
    expect(img.getAttribute("alt")).toBe("Session media abcdef012345");
    expect(img.getAttribute("width")).toBe("10");
    expect(link.querySelector(".media-animated")?.textContent).toBe("Animated still");
    expect(root().querySelector(".media-placeholder")?.textContent).toBe("Media pending");
  });

  it("renders a question with its answer options", () => {
    render({ items: [{ id: "q", kind: "question", title: "Pick one", calls: [{ title: "Yes", subtitle: "do it" }, { title: "No" }] }] });
    expect(root().querySelector(".question-subtitle")?.textContent).toBe("Answer in terminal");
    expect(Array.from(root().querySelectorAll(".question-option-title")).map((n) => n.textContent)).toEqual(["Yes", "No"]);
  });
});

describe("subagents and the native bridge", () => {
  const spawner: TranscriptItem = {
    id: "tool:9",
    kind: "tool",
    title: "Task",
    calls: [],
    subagentSummary: "2 agents · 4m12s",
    subagents: [
      { sessionId: "11111111-1111-4111-8111-111111111111", label: "Explore", toolCalls: 1 },
      { sessionId: "22222222-2222-4222-8222-222222222222", label: "Plan", toolCalls: 3 },
    ],
  };

  it("asks the app to open a worker", () => {
    const postMessage = vi.fn();
    (window as unknown as { webkit: unknown }).webkit = { messageHandlers: { longhouse: { postMessage } } };
    render({ items: [spawner] });
    const buttons = root().querySelectorAll<HTMLButtonElement>(".subagent-link");
    expect(Array.from(buttons).map((b) => b.textContent)).toEqual(["Explore1 call", "Plan3 calls"]);
    buttons[1].click();
    expect(postMessage).toHaveBeenCalledWith({ type: "openSubagent", sessionId: "22222222-2222-4222-8222-222222222222" });
  });

  it("stays inert without the bridge", () => {
    render({ items: [spawner] });
    expect(() => root().querySelector<HTMLButtonElement>(".subagent-link")!.click()).not.toThrow();
  });

  it("shows a submitted row's attachment summary and routes its actions to the app", () => {
    const postMessage = vi.fn();
    (window as unknown as { webkit: unknown }).webkit = { messageHandlers: { longhouse: { postMessage } } };
    render({
      items: [
        {
          id: "ios-failed",
          kind: "submitted",
          body: "describe this",
          status: "failed",
          subtitle: "Not delivered",
          attachments: [{ filename: "shot.jpg", mimeType: "image/jpeg", byteSize: 4 }],
        },
        { id: "ios-unknown", kind: "submitted", body: "again", status: "couldNotConfirm", subtitle: "Not confirmed" },
        { id: "ios-sent", kind: "submitted", body: "done", status: "sent", subtitle: "Sent" },
      ],
    });
    expect(root().querySelector('[data-testid="session-submitted-attachments"]')?.textContent).toBe(
      "Attachments · shot.jpg (image/jpeg, 4 bytes)",
    );
    const actions = Array.from(root().querySelectorAll<HTMLButtonElement>("[data-submitted-action]"));
    expect(actions.map((b) => b.textContent)).toEqual(["Edit", "Discard", "Retry send"]);
    actions[0].click();
    actions[2].click();
    expect(postMessage).toHaveBeenCalledWith({ type: "editSubmitted", clientRequestId: "ios-failed" });
    expect(postMessage).toHaveBeenCalledWith({ type: "retrySubmitted", clientRequestId: "ios-unknown" });
  });

  it("keeps an opened worker list open across renders", () => {
    render({ items: [spawner] });
    root().querySelector<HTMLDetailsElement>("details.subagents")!.open = true;
    render({ items: [spawner, { id: "p", kind: "message", role: "assistant", body: "next" }] });
    expect(root().querySelector<HTMLDetailsElement>("details.subagents")!.open).toBe(true);
  });
});

describe("scroll pinning", () => {
  it("pins to the bottom only while native says to stick", () => {
    render({ items: goldenItems }, { stick: true });
    expect(scrollTo).toHaveBeenCalled();
    scrollTo.mockClear();
    setStickToBottom(false);
    render({ items: goldenItems }, { stick: false });
    expect(scrollTo).not.toHaveBeenCalled();
  });
});

describe("the document entry", () => {
  it("installs the three bridge globals", async () => {
    await import("../main");
    expect(typeof window.renderTranscript).toBe("function");
    expect(typeof window.setStickToBottom).toBe("function");
    expect(typeof window.waitForTranscriptFrame).toBe("function");
  });
});
