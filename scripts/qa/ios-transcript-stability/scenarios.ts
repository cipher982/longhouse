// Each scenario plays one kind of content change against the transcript and
// reports numbers. A check fails the run when its value is over its budget.
import { assistantItem, conversation, streamingBody, toolItem, userItem, type Frame, type Harness, type Item } from "./lib.ts";

export interface Check {
  name: string;
  value: number;
  budget: number;
  unit: string;
  pass: boolean;
  /** Advisory checks are reported but never fail the run. */
  advisory?: boolean;
}

export interface Scenario {
  name: string;
  about: string;
  run(h: Harness): Promise<Check[]>;
}

const le = (name: string, value: number, budget: number, unit: string, advisory = false): Check => ({
  name,
  value: Math.round(value * 100) / 100,
  budget,
  unit,
  pass: value <= budget,
  advisory,
});

/**
 * Largest movement of the anchor row that the reader did not cause: where it
 * is minus where it started minus what they dragged. Infinity when it vanished.
 */
export function drift(frames: Frame[], top0: number): number {
  let worst = 0;
  for (const frame of frames) {
    if (frame.top === null) return Infinity;
    worst = Math.max(worst, Math.abs(frame.top - (top0 - frame.user)));
  }
  return worst;
}

export const maxGap = (frames: Frame[]): number => frames.reduce((worst, f) => Math.max(worst, Math.abs(f.gap)), 0);

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

function withLastAssistantBody(items: Item[], body: string): Item[] {
  const copy = items.map((item) => ({ ...item }));
  for (let i = copy.length - 1; i >= 0; i--) {
    if (copy[i].role === "assistant") {
      copy[i].body = `TOK_STREAM\n\n${body}`;
      break;
    }
  }
  return copy;
}

export const scenarios: Scenario[] = [
  {
    name: "stream_scrolled_up",
    about: "Reader is 2500 px up; the live message streams in at the bottom. The row they read must not move.",
    async run(h) {
      const base = conversation(30);
      await h.render(base, true);
      await h.scrollAway(2500);
      const token = await h.pickAnchor();
      const top0 = (await h.snap(token)).top!;
      await h.startSampling(token);
      for (let step = 0; step < 14; step++) {
        await h.render(withLastAssistantBody(base, streamingBody(step)), false);
        await h.settle(2);
      }
      const frames = await h.stopSampling();
      return [le("anchor drift", drift(frames, top0), 1, "pt")];
    },
  },
  {
    name: "stream_pinned",
    about: "Pinned at the bottom while the last message streams (code fence, list, table). The last line stays at the bottom edge every frame.",
    async run(h) {
      const base = conversation(30);
      await h.render(base, true);
      await h.settle(3);
      await h.startSampling("TOK_STREAM");
      for (let step = 0; step < 14; step++) {
        await h.render(withLastAssistantBody(base, streamingBody(step)), true);
        await h.settle(1);
      }
      await h.settle(3);
      return [le("max gap from bottom", maxGap(await h.stopSampling()), 1, "pt")];
    },
  },
  {
    name: "late_images_above_nodims",
    about: "Reader drags toward older messages through rows with images of undeclared size; images load above the row they read. That row must not move.",
    async run(h) {
      await h.render(conversation(30, { mediaEvery: 2, media: "nodims" }), true);
      await h.scrollAway(3400);
      const token = await h.pickAnchor();
      const top0 = (await h.snap(token)).top!;
      await h.startSampling(token);
      for (let step = 0; step < 24; step++) {
        await h.dragBy(-90);
        await sleep(110);
      }
      await sleep(600);
      return [le("anchor drift", drift(await h.stopSampling(), top0), 1, "pt")];
    },
  },
  {
    name: "late_images_above_dims",
    about: "Same drag with the server's width and height declared (the good case).",
    async run(h) {
      await h.render(conversation(30, { mediaEvery: 2, media: "dims" }), true);
      await h.scrollAway(3400);
      const token = await h.pickAnchor();
      const top0 = (await h.snap(token)).top!;
      await h.startSampling(token);
      for (let step = 0; step < 24; step++) {
        await h.dragBy(-90);
        await sleep(110);
      }
      await sleep(600);
      return [le("anchor drift", drift(await h.stopSampling(), top0), 1, "pt")];
    },
  },
  {
    name: "late_images_pinned",
    about: "Pinned at the bottom while images in the last rows load. The reader stays at the bottom.",
    async run(h) {
      const items = conversation(30);
      for (const i of [26, 27, 28, 29]) items[i * 2 + 1] = assistantItem(i, { media: "nodims" });
      await h.startSampling("TOK_A29");
      await h.render(items, true);
      await sleep(1800);
      return [le("max gap from bottom", maxGap(await h.stopSampling()), 1, "pt")];
    },
  },
  {
    name: "prepend_older_history",
    about: "Reader is 120 px from the top of the loaded tail; 50 older exchanges arrive above. Their row must not move.",
    async run(h) {
      await h.render(conversation(50, { from: 50 }), true);
      await h.scrollAway(120);
      const token = await h.pickAnchor();
      const top0 = (await h.snap(token)).top!;
      await h.startSampling(token);
      await h.render(conversation(100), false);
      await h.settle(4);
      const frames = await h.stopSampling();
      return [le("anchor drift", drift(frames, top0), 1, "pt")];
    },
  },
  {
    name: "prepend_while_pinned",
    about: "Pinned at the bottom when older history arrives above (open sequence). The reader stays at the bottom.",
    async run(h) {
      await h.render(conversation(20, { from: 40 }), true);
      await h.settle(3);
      await h.startSampling("TOK_A59");
      await h.render(conversation(60), true);
      await h.settle(4);
      return [le("max gap from bottom", maxGap(await h.stopSampling()), 1, "pt")];
    },
  },
  {
    name: "open_tool_receives_output",
    about: "A tool row the reader opened receives its output while they read below it. The row they read must not move.",
    async run(h) {
      const build = (state: "running" | "done"): Item[] => {
        const out: Item[] = [];
        for (let i = 0; i < 30; i++) {
          out.push(userItem(i), assistantItem(i, { code: false, paragraphs: 1 }));
          if (i === 4) out.push(toolItem(4, state, 40));
        }
        return out;
      };
      await h.render(build("running"), true);
      await h.page.evaluate(() => {
        for (const d of document.querySelectorAll("details.tool")) (d as HTMLDetailsElement).open = true;
      });
      await h.settle(2);
      // Scroll so the open tool is above the viewport and rows after it are read.
      const toolBottom = await h.page.evaluate(() => {
        const d = document.querySelector("details.tool")!;
        return d.getBoundingClientRect().bottom + window.scrollY;
      });
      await h.scrollAway(toolBottom + 40);
      const token = await h.pickAnchor();
      const top0 = (await h.snap(token)).top!;
      await h.startSampling(token);
      await h.render(build("done"), false);
      await h.settle(3);
      const frames = await h.stopSampling();
      return [le("anchor drift", drift(frames, top0), 1, "pt")];
    },
  },
  {
    name: "ui_state_survives_update",
    about: "Scrolled code block, open disclosure, text selection and untouched row nodes survive an unrelated update.",
    async run(h) {
      const build = (extra: number): Item[] => {
        const out: Item[] = [];
        for (let i = 0; i < 8; i++) {
          out.push(userItem(i), assistantItem(i, { paragraphs: 1 }));
          if (i === 2) out.push(toolItem(2, "done", 10));
        }
        for (let k = 0; k < extra; k++) out.push(assistantItem(100 + k, { paragraphs: 1 }));
        return out;
      };
      await h.render(build(0), false);
      await h.scrollAway(0);
      await h.page.evaluate(() => {
        const root = document.getElementById("root")!;
        const pre = root.querySelector("pre") as HTMLElement;
        pre.scrollLeft = 120;
        (root.querySelector("details.tool") as HTMLDetailsElement).open = true;
        const para = root.querySelector(".message-content p")!;
        const range = document.createRange();
        range.selectNodeContents(para);
        getSelection()!.removeAllRanges();
        getSelection()!.addRange(range);
        (window as any).__mark = {
          selection: getSelection()!.toString(),
          row: root.children[0],
        };
        (root.children[0] as any).__mark = true;
      });
      await h.render(build(1), false);
      await h.settle(2);
      const result = await h.page.evaluate(() => {
        const root = document.getElementById("root")!;
        return {
          scrollLeft: (root.querySelector("pre") as HTMLElement).scrollLeft,
          open: (root.querySelector("details.tool") as HTMLDetailsElement).open,
          selection: getSelection()!.toString() === (window as any).__mark.selection && getSelection()!.toString().length > 0,
          identity: (root.children[0] as any).__mark === true,
        };
      });
      const lost = (ok: boolean) => (ok ? 0 : 1);
      return [
        le("code block scroll lost", lost(result.scrollLeft === 120), 0, "bool"),
        le("open disclosure lost", lost(result.open), 0, "bool"),
        le("selection lost", lost(result.selection), 0, "bool"),
        le("row node replaced", lost(result.identity), 0, "bool"),
      ];
    },
  },
  {
    name: "keyboard_resize_pinned",
    about: "The viewport shrinks (keyboard) and grows back while pinned. The last line stays at the bottom edge.",
    async run(h) {
      await h.render(conversation(30), true);
      await h.settle(3);
      await h.startSampling("TOK_A29");
      await h.page.setViewportSize({ width: 390, height: 330 });
      await h.settle(4);
      const during = (await h.snap("TOK_A29")).gap;
      await h.page.setViewportSize({ width: 390, height: 640 });
      await h.settle(4);
      const frames = await h.stopSampling();
      return [le("gap with keyboard up", Math.abs(during), 1, "pt"), le("max gap, whole sequence (settled frames)", maxGap(frames.slice(-3)), 1, "pt")];
    },
  },
  {
    name: "expand_row_in_view",
    about: "Reader expands a collapsed message in view. The row's top does not move; it grows downward.",
    async run(h) {
      const items = conversation(10);
      items[7] = { ...items[7], collapsed: true, fullBody: `${items[7].body}\n\n${"More text that was cut. ".repeat(80)}` };
      await h.render(items, true);
      await h.scrollAway(0);
      await h.page.evaluate(() => {
        const button = document.querySelector("[data-expand-index]") as HTMLElement;
        button.scrollIntoView({ block: "center" });
      });
      await h.settle(2);
      const token = await h.page.evaluate(() => {
        const button = document.querySelector("[data-expand-index]")!;
        return button.closest("#root > *")!.textContent!.match(/TOK_A\d+/)![0];
      });
      const top0 = (await h.snap(token)).top!;
      await h.startSampling(token);
      await h.page.evaluate(() => (document.querySelector("[data-expand-index]") as HTMLElement).click());
      await h.settle(3);
      return [le("row top drift", drift(await h.stopSampling(), top0), 1, "pt")];
    },
  },
  {
    name: "stream_while_dragging",
    about: "The reader drags toward older messages while the live message streams at 20 Hz. The row they read moves only with their finger.",
    async run(h) {
      const base = conversation(40, { mediaEvery: 3, media: "dims" });
      await h.render(base, true);
      await h.scrollAway(4200);
      const token = await h.pickAnchor();
      const top0 = (await h.snap(token)).top!;
      await h.startSampling(token);
      for (let step = 0; step < 40; step++) {
        await h.dragBy(-35);
        await h.render(withLastAssistantBody(base, streamingBody(step)), false);
        await sleep(50);
      }
      await h.settle(3);
      return [le("anchor drift", drift(await h.stopSampling(), top0), 1, "pt")];
    },
  },
  {
    name: "burst_updates_one_frame",
    about: "Five renders land in one frame (a catch-up after the app resumes). Pinned stays pinned; a reader stays put.",
    async run(h) {
      const base = conversation(30);
      await h.render(base, true);
      await h.settle(2);
      await h.startSampling("TOK_STREAM");
      for (let step = 0; step < 5; step++) await h.render(withLastAssistantBody(base, streamingBody(step * 2)), true);
      await h.settle(3);
      const pinnedGap = maxGap(await h.stopSampling());
      await h.scrollAway(2600);
      const token = await h.pickAnchor();
      const top0 = (await h.snap(token)).top!;
      await h.startSampling(token);
      for (let step = 0; step < 5; step++) await h.render(withLastAssistantBody(base, streamingBody(step * 2 + 1)), false);
      await h.settle(3);
      return [le("pinned gap", pinnedGap, 1, "pt"), le("reader anchor drift", drift(await h.stopSampling(), top0), 1, "pt")];
    },
  },
  {
    name: "error_banner_while_reading",
    about: "A refresh-failed banner appears above the transcript while the reader is mid-history. Their row must not move.",
    async run(h) {
      const base = conversation(30);
      await h.render(base, true);
      await h.scrollAway(2600);
      const token = await h.pickAnchor();
      const top0 = (await h.snap(token)).top!;
      await h.startSampling(token);
      await h.render(base, false, "Refresh failed: the server did not answer");
      await h.settle(3);
      const shown = await h.page.evaluate(() => !!document.querySelector("#root > .error"));
      await h.render(base, false, null);
      await h.settle(3);
      return [le("anchor drift", drift(await h.stopSampling(), top0), 1, "pt"), le("banner missing", shown ? 0 : 1, 0, "bool")];
    },
  },
  {
    name: "width_change_reading",
    about: "The viewport widens (rotation, split view) and text reflows. The row at the top of the screen stays at the top.",
    async run(h) {
      await h.render(conversation(30), true);
      await h.scrollAway(2600);
      // The topmost visible row is the page's anchor; rows below it move when it reflows.
      const token = await h.pickTopAnchor();
      const top0 = (await h.snap(token)).top!;
      await h.startSampling(token);
      await h.page.setViewportSize({ width: 844, height: 390 });
      await h.settle(4);
      await h.page.setViewportSize({ width: 390, height: 640 });
      await h.settle(4);
      return [le("anchor drift", drift(await h.stopSampling(), top0), 1.5, "pt")];
    },
  },
  {
    name: "lite_body_arrives_above",
    about: "A cut tool row above the reader gets its full body (a re-render with the open row's output grown). The reader's row stays put.",
    async run(h) {
      const build = (lines: number): Item[] => {
        const out: Item[] = [];
        for (let i = 0; i < 25; i++) {
          out.push(userItem(i), assistantItem(i, { code: false, paragraphs: 1 }));
          if (i === 3) out.push({ ...toolItem(3, "done", lines), bodyCursors: ["c1"], bodyState: lines > 3 ? null : "preview" });
        }
        return out;
      };
      await h.render(build(3), true);
      await h.page.evaluate(() => ((document.querySelector("details.tool") as HTMLDetailsElement).open = true));
      await h.settle(2);
      const bottom = await h.page.evaluate(() => document.querySelector("details.tool")!.getBoundingClientRect().bottom + scrollY);
      await h.scrollAway(bottom + 60);
      const token = await h.pickAnchor();
      const top0 = (await h.snap(token)).top!;
      await h.startSampling(token);
      await h.render(build(60), false);
      await h.settle(3);
      return [le("anchor drift", drift(await h.stopSampling(), top0), 1, "pt")];
    },
  },
  {
    name: "stream_update_cost",
    about:
      "Synchronous cost of one update (decode, DOM, forced layout) on a 120-row transcript. Advisory: machine-dependent, and paint is excluded because headless WebKit paces frames at 130-400 ms even for an empty page.",
    async run(h) {
      const base = conversation(60);
      await h.render(base, true);
      const costs: number[] = [];
      for (let step = 0; step < 40; step++) {
        const items = withLastAssistantBody(base, streamingBody(step));
        const b64 = Buffer.from(JSON.stringify({ errorMessage: null, items })).toString("base64");
        costs.push(
          await h.page.evaluate(
            ([b, q]) => {
              const t0 = performance.now();
              (window as any).renderTranscript(b, true, q, "x");
              document.body.offsetHeight;
              return performance.now() - t0;
            },
            [b64, 10_000 + step] as const,
          ),
        );
        await sleep(30);
      }
      costs.sort((a, b) => a - b);
      return [le("p95 sync update", costs[Math.floor(costs.length * 0.95)], 16.7, "ms", true)];
    },
  },
];
