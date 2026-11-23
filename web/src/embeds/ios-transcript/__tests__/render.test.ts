import { readFileSync } from "node:fs";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderTranscript, resetTranscriptStateForTesting, waitForTranscriptFrame } from "../render";
import { setStickToBottom } from "../scroll";
import type { TranscriptItem, TranscriptPayload } from "../types";

// The golden rows `presentTranscript` produces for the hostile transcript
// fixture (see presentTranscript.test.ts). Rendering them here closes the loop
// from projection to DOM.
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
    expect(Object.keys(metrics).sort()).toEqual(["decode_ms", "dom_ms", "html_ms", "latest_item_id", "present_ms", "row_count"]);

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

  it("renders the same DOM whether built fresh or updated in place", () => {
    render({ items: goldenItems });
    const fresh = root().innerHTML;
    // A second, identical payload touches nothing.
    const before = rows();
    render({ items: goldenItems });
    expect(rows()).toEqual(before);
    rows().forEach((node, index) => expect(node).toBe(before[index]));
    expect(root().innerHTML).toBe(fresh);
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

describe("in-place updates", () => {
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
    render({ items: [item("a", "first"), item("b", "second")] });
    const [a, b] = rows();

    render({ items: [item("a", "first"), item("b", "second, edited"), item("c", "third")] });
    const [a2, b2, c2] = rows();
    expect(a2).toBe(a);
    expect(b2).not.toBe(b);
    expect(b2.textContent).toContain("second, edited");
    expect(c2.textContent).toContain("third");
  });

  it("re-indexes a reused node when history is prepended", () => {
    render({ items: [item("b", "later", { collapsed: true, fullBody: "later, in full" })] });
    const reused = rows()[0];
    expect(reused.getAttribute("data-message-index")).toBe("0");

    render({ items: [item("a", "earlier"), item("b", "later", { collapsed: true, fullBody: "later, in full" })] });
    expect(rows()[1]).toBe(reused);
    expect(reused.getAttribute("data-message-index")).toBe("1");
    expect(reused.querySelector("[data-expand-index]")?.getAttribute("data-expand-index")).toBe("1");

    // The expand handler follows the new index to the right item.
    (reused.querySelector(".expand") as HTMLButtonElement).click();
    expect(reused.querySelector(".message-content")?.innerHTML).toBe("<p>later, in full</p>");
    expect(reused.querySelector(".expand")).toBeNull();
  });

  it("drops nodes for items that left the payload", () => {
    render({ items: [item("a", "one"), item("b", "two")] });
    render({ items: [item("b", "two")] });
    expect(rows()).toHaveLength(1);
    expect(rows()[0].textContent).toContain("two");
  });
});

describe("rows that render to several nodes", () => {
  const tool = (id: string, extra: Partial<TranscriptItem> = {}): TranscriptItem => ({
    id,
    kind: "tool",
    title: "Bash",
    subtitle: "npm test",
    status: "failed",
    failurePreview: "boom",
    input: "npm test",
    ...extra,
  });

  it("keeps every node of an item (the tool and its failure preview) and replaces them together", () => {
    render({ items: [tool("t")] });
    expect(rows().map((n) => n.className)).toEqual(["tool row", "failure-preview"]);
    const [details, preview] = rows();

    render({ items: [tool("t"), { id: "m", kind: "message", role: "assistant", body: "after" }] });
    expect(rows()[0]).toBe(details);
    expect(rows()[1]).toBe(preview);

    render({ items: [tool("t", { failurePreview: "boom, differently" }), { id: "m", kind: "message", role: "assistant", body: "after" }] });
    expect(rows()[0]).not.toBe(details);
    expect(rows().map((n) => n.className)).toEqual(["tool row", "failure-preview", "row message assistant"]);
    expect(rows()[1].textContent).toContain("differently");
  });

  it("keeps a disclosure the reader opened when its row is rebuilt", () => {
    render({ items: [tool("t", { status: "running", failurePreview: null })] });
    rows()[0].setAttribute("open", "");
    render({ items: [tool("t", { status: "ok", failurePreview: null, output: "done" })] });
    expect((rows()[0] as HTMLDetailsElement).open).toBe(true);
    expect(rows()[0].textContent).toContain("done");
  });

  it("keeps a message the reader expanded when an unrelated item changes", () => {
    const collapsed = { id: "c", kind: "message", role: "assistant", body: "short", collapsed: true, fullBody: "short, then the rest" } as TranscriptItem;
    render({ items: [collapsed, { id: "x", kind: "message", role: "assistant", body: "one" }] });
    (rows()[0].querySelector(".expand") as HTMLButtonElement).click();
    render({ items: [collapsed, { id: "x", kind: "message", role: "assistant", body: "two" }] });
    expect(rows()[0].textContent).toContain("the rest");
  });
});

describe("rows", () => {
  it("expands a collapsed user message to its full text", () => {
    render({ items: [{ id: "u", kind: "message", role: "user", body: "short…", fullBody: "<the whole thing>", collapsed: true }] });
    (root().querySelector(".expand") as HTMLButtonElement).click();
    expect(root().querySelector(".bubble")?.textContent).toBe("<the whole thing>");
  });

  it("shows an attached-images chip instead of the engine instruction", () => {
    render({ items: [{ id: "u", kind: "message", role: "user", body: "look", attachedImages: 2, collapsed: false }] });
    expect(root().querySelector(".bubble")?.textContent).toBe("look");
    expect(root().querySelector("[data-testid=session-attached-images]")?.textContent).toBe("2 images attached");
  });

  it("shows attached images as thumbnails when their URLs are known", () => {
    render({
      items: [
        { id: "u", kind: "message", role: "user", body: "look", attachedImages: 2, attachmentThumbs: ["https://h/a/0", "https://h/a/1"], collapsed: false },
      ],
    });
    const imgs = root().querySelectorAll("[data-testid=session-attached-thumbs] img");
    expect(Array.from(imgs).map((i) => i.getAttribute("src"))).toEqual(["https://h/a/0", "https://h/a/1"]);
    expect(root().querySelector("[data-testid=session-attached-images]")).toBeNull();
  });

  describe("reasoning", () => {
    it("shows a provider's reasoning summary as readable prose with the full thought behind a tap", () => {
      render({
        items: [
          { id: "reasoning:1", kind: "reasoning", body: "**Checking the manifest**\nThe byte offset is 4 <b>bits</b>." },
        ],
      });

      const row = root().querySelector("details.reasoning") as HTMLDetailsElement;
      expect(row.getAttribute("data-testid")).toBe("session-reasoning");
      expect(row.open).toBe(false);
      expect(row.querySelector(".reasoning-prose")?.textContent).toBe(
        "Checking the manifest\nThe byte offset is 4 <b>bits</b>.",
      );
      expect(row.querySelector("b")).toBeNull();
    });
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
    it("marks wake notices on their row", () => {
      render({ items: [{ ...jobResult, wake: true }] });
      expect(root().querySelector("details.tool.notice")?.getAttribute("data-wake")).toBe("true");

      render({
        items: [
          {
            ...notice("wake-run", 'Background command "Run the checks" completed (exit code 0) (3 updates)'),
            wake: true,
          },
        ],
      });
      expect(root().querySelector("div.tool.notice.static")?.getAttribute("data-wake")).toBe("true");
    });


    it("shows Longhouse's own close notice whole, even past the header length cap", () => {
      const text =
        "Stopped 1 background task: tail the dev server log. Longhouse couldn't confirm all its " +
        "processes exited, and left any that remain alone.";
      expect(text.length).toBeGreaterThan(120);
      render({ items: [{ id: "close", kind: "providerNotification", body: text, origin: "longhouse" }] });

      expect(root().querySelector("details")).toBeNull();
      const row = root().querySelector('div.tool.notice.static[data-origin="longhouse"]')!;
      expect(row.querySelector(".tool-title")?.textContent).toBe(text);
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
    expect(root().querySelector(".question-subtitle")?.textContent).toBe("Waiting for your answer");
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

  it("draws a submitted row's image from the phone's preview before the echo lands", () => {
    const thumb = "data:image/jpeg;base64,/9j/AAAA";
    render({
      items: [
        {
          id: "ios-sending",
          kind: "submitted",
          body: "look",
          status: "submitting",
          subtitle: "Sending",
          attachments: [
            { filename: "a.jpg", mimeType: "image/jpeg", byteSize: 4, thumbDataUrl: thumb },
            { filename: "b.jpg", mimeType: "image/jpeg", byteSize: 4, thumbDataUrl: "javascript:alert(1)" },
          ],
        },
      ],
    });
    const imgs = Array.from(root().querySelectorAll<HTMLImageElement>('[data-testid="session-submitted-thumbs"] img'));
    expect(imgs.map((img) => img.getAttribute("src"))).toEqual([thumb]);
    expect(root().querySelector('[data-testid="session-submitted-attachments"]')?.textContent).toBe("1 image attached");
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
    expect(root().querySelector('[data-testid="session-submitted-attachments"]')?.textContent).toBe("1 image attached");
    const actions = Array.from(root().querySelectorAll<HTMLButtonElement>("[data-submitted-action]"));
    expect(actions.map((b) => b.textContent)).toEqual(["Edit", "Discard", "Retry send"]);
    actions[0].click();
    actions[2].click();
    expect(postMessage).toHaveBeenCalledWith({ type: "editSubmitted", clientRequestId: "ios-failed" });
    expect(postMessage).toHaveBeenCalledWith({ type: "retrySubmitted", clientRequestId: "ios-unknown" });
  });

  it("asks the app for a cut row's full bodies when it opens, and keeps it open when they arrive", () => {
    const postMessage = vi.fn();
    (window as unknown as { webkit: unknown }).webkit = { messageHandlers: { longhouse: { postMessage } } };
    const preview: TranscriptItem = {
      id: "tool:5",
      kind: "tool",
      title: "Bash",
      output: "line 1\nline 2\n… 40 more lines …",
      calls: [],
      bodyCursors: ["c-call", "c-result"],
      bodyState: "preview",
    };
    render({ items: [preview] });
    const row = root().querySelector<HTMLDetailsElement>("details.tool")!;
    expect(row.querySelector(".body-note")?.textContent).toBe("This is a preview of the output.");
    row.open = true;
    row.dispatchEvent(new Event("toggle"));
    expect(postMessage).toHaveBeenCalledWith({ type: "loadToolBodies", cursors: ["c-call", "c-result"] });

    render({ items: [{ ...preview, output: "the whole output", bodyCursors: null, bodyState: null }] });
    const loaded = root().querySelector<HTMLDetailsElement>("details.tool")!;
    expect(loaded.open).toBe(true);
    expect(loaded.querySelector(".body-note")).toBeNull();
    expect(loaded.textContent).toContain("the whole output");
  });

  it("says when loading a cut row's full body failed", () => {
    render({
      items: [{ id: "tool:7", kind: "tool", title: "Read", calls: [], bodyCursors: ["c"], bodyState: "failed" }],
    });
    expect(root().querySelector(".body-note")?.textContent).toBe(
      "Couldn't load the full output; this is a preview. Reopen the row in a few seconds to try again.",
    );
  });

  it("says when a cut row's full body is gone", () => {
    render({
      items: [{ id: "tool:6", kind: "tool", title: "Read", calls: [], bodyCursors: ["c"], bodyState: "unavailable" }],
    });
    expect(root().querySelector(".body-note")?.textContent).toBe(
      "The full output is no longer available; this is a preview.",
    );
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
    // jsdom has no layout: give the document a height to be pinned to.
    Object.defineProperty(document.documentElement, "scrollHeight", { configurable: true, value: 5000 });
    render({ items: goldenItems }, { stick: true });
    expect(scrollTo).toHaveBeenCalled();
    scrollTo.mockClear();
    setStickToBottom(false);
    render({ items: goldenItems }, { stick: false });
    expect(scrollTo).not.toHaveBeenCalled();
    delete (document.documentElement as unknown as { scrollHeight?: number }).scrollHeight;
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

describe("compaction boundaries", () => {
  it("renders compaction boundaries as divider rows", () => {
    render({
      items: [
        {
          id: "compaction-boundary:1",
          kind: "compaction",
          body: "Conversation compacted [trigger=auto pre_tokens=155708]",
        },
      ],
    });

    const row = root().querySelector('[data-testid="session-compaction-boundary"]');
    expect(row).not.toBeNull();
    expect(row?.textContent).toContain("Context compacted");
    expect(row?.textContent).toContain("Auto · 155.7k tokens");
  });

  it("renders microcompact boundaries with tokens saved", () => {
    render({
      items: [
        {
          id: "compaction-boundary:2",
          kind: "compaction",
          body: "Context microcompacted [trigger=auto tokens_saved=14200]",
        },
        {
          id: "compaction-boundary:3",
          kind: "compaction",
          body: "Microcompacted tool outputs [trigger=auto pre_tokens=180000 tokens_saved=42000]",
        },
      ],
    });

    const rows = root().querySelectorAll('[data-testid="session-compaction-boundary"]');
    expect(rows).toHaveLength(2);
    expect(rows[0]?.textContent).toContain("Context microcompacted");
    expect(rows[0]?.textContent).toContain("Auto · 14.2k saved");
    expect(rows[1]?.textContent).toContain("Context microcompacted");
    expect(rows[1]?.textContent).toContain("Auto · 180k tokens · 42k saved");
  });
});

describe("Claude fidelity signals", () => {
  it("keeps turnEnd visible outside collapsed details rows", () => {
    render({
      items: [
        {
          id: "tool:9",
          kind: "tool",
          title: "Bash",
          subtitle: "pytest",
          status: "done",
          duration: "2s",
          calls: [],
          turnEnd: { label: "Worked for 12s", doneAt: "Turn finished 9:15 AM" },
        },
      ],
    });

    const details = root().querySelector("details.tool");
    const turnEnd = root().querySelector('[data-testid="session-turn-end"]');
    expect(details).not.toBeNull();
    expect(turnEnd).not.toBeNull();
    expect(details?.contains(turnEnd!)).toBe(false);
    expect(turnEnd?.textContent).toBe("✻ Worked for 12s · Turn finished 9:15 AM");
  });

  it("renders commentary phase, withheld reasoning, task notifications, waiting notice, and api errors", () => {
    render({
      items: [
        { id: "r1", kind: "reasoning", body: "", reasoningWithheld: true },
        { id: "m1", kind: "message", role: "assistant", body: "Launching audit workers.", phase: "commentary" },
        { id: "w1", kind: "providerNotification", waitingBackgroundAgents: 3 },
        {
          id: "tn1",
          kind: "providerNotification",
          taskNotification: {
            task_id: "a101",
            status: "completed",
            summary: 'Agent "Laptop release" completed',
            duration_ms: 1_613_000,
            subagent_tokens: 94_200,
            tool_uses: 18,
            note: "TestFlight build queued",
            result: "Uploaded internal build 0.1.81",
          },
        },
        {
          id: "e1",
          kind: "providerNotification",
          apiError: { retry_attempt: 1, max_retries: 4, status_code: 529, retry_in_ms: 4000, error: "Overloaded" },
        },
      ],
    });

    expect(root().querySelector('[data-testid="session-reasoning-withheld"]')?.textContent?.trim()).toBe(
      "Reasoning withheld",
    );
    expect(root().querySelector(".message.assistant.commentary")).not.toBeNull();
    expect(root().querySelector(".message.assistant.commentary .phase-badge")).toBeNull();
    expect(root().querySelector(".message.assistant.commentary .message-content")?.textContent).toContain(
      "Launching audit workers.",
    );
    expect(root().querySelector('[data-testid="session-waiting-background-agents"]')?.textContent).toContain(
      "Waiting for 3 background agents to finish",
    );
    const taskNotice = root().querySelector('[data-testid="session-task-notification"]');
    expect(taskNotice?.textContent).toContain('Agent "Laptop release" completed');
    expect(taskNotice?.textContent).toContain("26m 53s · 94.2k tokens · 18 tool uses");
    expect(taskNotice?.textContent).toContain("Uploaded internal build 0.1.81");
    const apiErr = root().querySelector('[data-testid="session-api-error-notice"]');
    expect(apiErr?.textContent).toContain("retry 1/4 · HTTP 529 · in 4s");
    expect(apiErr?.textContent).toContain("Overloaded");
  });

  it("leads a narration step with its intent, since the step drops its tool title", () => {
    render({
      items: [
        {
          id: "tool:trail",
          kind: "tool",
          title: "Bash",
          subtitle: "uv run pytest tests/test_refresh.py",
          status: "completed",
          toolIntent: "Run the refresh test",
          narrationTrail: true,
          calls: [],
        } as TranscriptItem,
      ],
    });

    const row = root().querySelector("details.tool.trail");
    expect(row).not.toBeNull();
    expect(row?.querySelector(".tool-title")).toBeNull();
    expect(row?.querySelector(".tool-subtitle")?.textContent).toBe("Run the refresh test");
  });

  it("renders running tool intent, live timer attribute, output tail, and nested subagents", () => {
    const postMessage = vi.fn();
    (window as unknown as { webkit: unknown }).webkit = { messageHandlers: { longhouse: { postMessage } } };
    const fiveSecondsAgo = Date.now() - 5000;
    render({
      items: [
        {
          id: "tool:run",
          kind: "tool",
          title: "Bash",
          subtitle: "make test-e2e-core",
          status: "running",
          startedAtMs: fiveSecondsAgo,
          toolIntent: "Running browser suite",
          output: "line 1\nline 2\nline 3",
          calls: [],
          subagentSummary: "2 agents",
          subagents: [
            {
              sessionId: "sub-1",
              label: "Inspect parser",
              toolCalls: 14,
              agentType: "Explore",
              liveStatus: "running",
              durationMs: 1_860_000,
              totalTokens: 116_500,
            },
            {
              sessionId: "sub-2",
              label: "Verify meta lineage",
              toolCalls: 9,
              agentType: "general-purpose",
              isFork: true,
              liveStatus: "waiting",
              depth: 1,
            },
          ],
        },
      ],
    });

    const meta = root().querySelector("[data-started-at-ms]");
    expect(meta).not.toBeNull();
    expect(meta?.textContent).toMatch(/^0:0[4-6]$/);
    expect(root().querySelector(".tool-intent")?.textContent).toBe("Running browser suite");
    expect(root().querySelector(".running-output-tail")?.textContent).toBe("line 2\nline 3");
    const nested = root().querySelector('li[data-depth="1"]');
    expect(nested?.textContent).toContain("general-purpose");
    expect(nested?.textContent).toContain("fork");
    expect(nested?.textContent).toContain("waiting");
    root().querySelector<HTMLButtonElement>('[data-subagent-session="sub-2"]')?.click();
    expect(postMessage).toHaveBeenCalledWith({ type: "openSubagent", sessionId: "sub-2" });
  });
});
