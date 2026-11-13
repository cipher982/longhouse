#!/usr/bin/env bun
/**
 * Web transcript stability check.
 *
 * Plays realistic live-streaming tool and prose updates against SessionDetailPage
 * in headless Chromium and samples every animation frame (rAF + PerformanceObserver
 * layout-shift) to catch anchor drift, tool-well height jumps, and CLS.
 *
 *   bunx tsx scripts/qa/web-transcript-stability.ts [--only=name,name] [--out=dir]
 */
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { chromium, type Browser, type BrowserContext, type Page } from "playwright";
import { ensureFrontend } from "../ui/frontend";
import { buildRailSessionsFixture, buildSessionDetailStressFixture } from "../ui-fixtures/sessionDetailStress";

interface Check {
  name: string;
  value: number;
  budget: number;
  unit: string;
  pass: boolean;
  advisory?: boolean;
}

interface FrameSample {
  t: number;
  scrollTop: number;
  scrollHeight: number;
  clientHeight: number;
  anchorTop: number | null;
  toolWellHeight: number | null;
  clsTotal: number;
  tail?: string;
}

interface Scenario {
  name: string;
  about: string;
  run(h: WebStabilityHarness): Promise<Check[]>;
}

const arg = (name: string) => process.argv.find((a) => a.startsWith(`--${name}=`))?.split("=")[1];
const only = arg("only")?.split(",").filter(Boolean);
const stamp = new Date().toISOString().replace(/[-:]/g, "").slice(0, 15);
const outDir = arg("out") ?? path.join("/tmp/agents/web-transcript-stability", stamp);
mkdirSync(outDir, { recursive: true });

const le = (name: string, value: number, budget: number, unit: string, advisory = false): Check => ({
  name,
  value: Math.round(value * 1000) / 1000,
  budget,
  unit,
  pass: value <= budget,
  advisory,
});

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

const PORT = Number(process.env.WEB_STABILITY_PORT ?? 47299);
const BASE_URL = process.env.FRONTEND_URL ?? `http://127.0.0.1:${PORT}`;

const INIT_SCRIPT = `
(() => {
  // Controllable EventSource wrapper so the harness can push workspace_changed events.
  const NativeEventSource = window.EventSource;
  const instances = [];
  class MockEventSource {
    constructor(url, opts) {
      this.url = String(url);
      this.withCredentials = Boolean(opts && opts.withCredentials);
      this.readyState = 1;
      this.listeners = new Map();
      instances.push(this);
      setTimeout(() => {
        this._dispatch("connected", { session_id: "session-detail-stress", server_now_ms: Date.now() });
      }, 10);
    }
    addEventListener(type, fn) {
      const set = this.listeners.get(type) || new Set();
      set.add(fn);
      this.listeners.set(type, set);
    }
    removeEventListener(type, fn) {
      const set = this.listeners.get(type);
      if (set) set.delete(fn);
    }
    close() {
      this.readyState = 2;
    }
    _dispatch(type, payload) {
      const set = this.listeners.get(type);
      if (!set) return;
      const ev = new MessageEvent(type, { data: JSON.stringify(payload) });
      for (const fn of set) fn(ev);
    }
  }
  window.EventSource = function(url, opts) {
    if (String(url).includes("/workspace/stream")) {
      return new MockEventSource(url, opts);
    }
    return new NativeEventSource(url, opts);
  };
  window.__emitWorkspaceChanged = (payload) => {
    for (const inst of instances) {
      if (inst.readyState === 1) inst._dispatch("workspace_changed", payload);
    }
  };

  // Frame-by-frame rAF + LayoutShift sampler.
  let clsTotal = 0;
  try {
    const po = new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) {
        if (!entry.hadRecentInput) clsTotal += entry.value;
      }
    });
    po.observe({ type: "layout-shift", buffered: true });
  } catch {}

  let sampling = false;
  let frames = [];
  let anchorToken = null;
  let rafId = 0;

  function findRowByToken(token) {
    if (!token) return null;
    const rows = document.querySelectorAll('[data-testid="session-timeline-row"]');
    for (const r of rows) {
      if ((r.textContent || "").includes(token)) return r;
    }
    return null;
  }

  function readSample() {
    const scroller = document.querySelector(".timeline-pane__list");
    const row = findRowByToken(anchorToken);
    const previews = document.querySelectorAll('[data-testid="tool-output-preview"], [data-testid="tool-failure-preview"]');
    const lastPreview = previews.length ? previews[previews.length - 1] : null;
    const allRows = document.querySelectorAll('[data-testid="session-timeline-row"]');
    const tail = Array.from(allRows).slice(-3).map((el) => {
      const r = el.getBoundingClientRect();
      const pv = el.querySelector(".tl-tool-preview");
      return el.getAttribute("data-row-kind") + ":" + Math.round(r.height) + (pv ? "[" + pv.className.replace(/tl-tool-preview/g, "pv").trim() + ":" + Math.round(pv.getBoundingClientRect().height) + "]" : "");
    }).join(",");
    return {
      t: performance.now(),
      scrollTop: scroller ? scroller.scrollTop : 0,
      scrollHeight: scroller ? scroller.scrollHeight : 0,
      clientHeight: scroller ? scroller.clientHeight : 0,
      anchorTop: row ? row.getBoundingClientRect().top : null,
      toolWellHeight: lastPreview ? lastPreview.getBoundingClientRect().height : null,
      clsTotal,
      tail,
    };
  }

  function tick() {
    if (!sampling) return;
    frames.push(readSample());
    rafId = requestAnimationFrame(tick);
  }

  window.__sampler = {
    resetCls() {
      clsTotal = 0;
    },
    start(token) {
      anchorToken = token;
      frames = [readSample()];
      sampling = true;
      rafId = requestAnimationFrame(tick);
    },
    stop() {
      sampling = false;
      cancelAnimationFrame(rafId);
      frames.push(readSample());
      return frames;
    },
    snap(token) {
      anchorToken = token;
      return readSample();
    },
  };
})();
`;

class WebStabilityHarness {
  private context!: BrowserContext;
  page!: Page;
  private fixture = buildSessionDetailStressFixture();
  private workspaceState = structuredClone(this.fixture.workspace);
  private nextEventId = 900_000;
  private nextPrependId = 100;
  private sessionId = this.fixture.session.id;

  async open(browser: Browser): Promise<void> {
    this.fixture = buildSessionDetailStressFixture();
    this.workspaceState = structuredClone(this.fixture.workspace);
    this.sessionId = this.fixture.session.id;
    const items = this.workspaceState.projection.items as Array<{ kind: string; session_id: string; timestamp: string; event?: Record<string, unknown> | null }>;
    const maxExistingId = items.reduce((m, item) => Math.max(m, Number(item.event?.id ?? 0)), 0);
    this.nextEventId = maxExistingId + 10;

    this.context = await browser.newContext({
      viewport: { width: 1440, height: 900 },
      reducedMotion: "reduce",
    });
    await this.context.addInitScript(INIT_SCRIPT);

    const sessionBasePath = `/api/timeline/sessions/${this.sessionId}`;
    await this.context.route(`${BASE_URL}/api/**`, async (route) => {
      const pathname = new URL(route.request().url()).pathname;
      if (pathname === sessionBasePath) {
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(this.fixture.session) });
      }
      if (pathname === `${sessionBasePath}/thread`) {
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(this.fixture.thread) });
      }
      if (pathname === `${sessionBasePath}/workspace`) {
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(this.workspaceState) });
      }
      if (pathname === `${sessionBasePath}/projection`) {
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(this.workspaceState.projection) });
      }
      if (pathname === `${sessionBasePath}/turns`) {
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(this.fixture.turns) });
      }
      if (pathname === `${sessionBasePath}/subagents`) {
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ subagents: [] }) });
      }
      if (pathname === `/api/sessions/${this.sessionId}/lock`) {
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ locked: false }) });
      }
      if (pathname === `/api/sessions/${this.sessionId}/inputs`) {
        return route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
      }
      if (pathname === "/api/timeline/sessions") {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(buildRailSessionsFixture(this.fixture.session)),
        });
      }
      if (pathname === "/api/auth/status") {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            authenticated: true,
            user: { id: 1, email: "qa@longhouse.ai", display_name: "QA", role: "admin" },
          }),
        });
      }
      if (pathname === "/api/auth/methods") {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ google: false, password: false, sso: false, sso_url: null }),
        });
      }
      if (pathname === "/api/auth/me") {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ id: 1, email: "qa@longhouse.ai", name: "QA", role: "admin" }),
        });
      }
      return route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
    });

    this.page = await this.context.newPage();
    this.page.on("pageerror", (err) => console.error("[pageerror]", err.message));
    this.page.on("console", (msg) => {
      if (msg.type() === "error") console.error("[console.error]", msg.text());
    });
    await this.page.goto(`${BASE_URL}/timeline/${this.sessionId}`, { waitUntil: "domcontentloaded" });
    try {
      await this.page.locator('[data-testid="session-timeline-row"]').first().waitFor({ state: "visible", timeout: 10_000 });
    } catch (err) {
      const url = this.page.url();
      const text = await this.page.locator("body").innerText().catch(() => "");
      console.error("[open failed] url=", url, "body=", JSON.stringify(text.slice(0, 400)));
      throw err;
    }
    await this.settle(6);
  }

  async close(): Promise<void> {
    await this.context?.close();
  }

  async settle(frames = 3): Promise<void> {
    await this.page.evaluate(
      (n) =>
        new Promise<void>((resolve) => {
          let left = n;
          const step = () => {
            left -= 1;
            if (left <= 0) resolve();
            else requestAnimationFrame(step);
          };
          requestAnimationFrame(step);
        }),
      frames,
    );
  }

  async scrollUpBy(px: number): Promise<void> {
    await this.page.evaluate((delta) => {
      const scroller = document.querySelector(".timeline-pane__list") as HTMLElement | null;
      if (!scroller) throw new Error("missing .timeline-pane__list");
      scroller.scrollTop = Math.max(0, scroller.scrollHeight - scroller.clientHeight - delta);
    }, px);
    await this.settle(3);
  }

  async scrollToBottom(): Promise<void> {
    await this.page.evaluate(() => {
      const scroller = document.querySelector(".timeline-pane__list") as HTMLElement | null;
      if (!scroller) throw new Error("missing .timeline-pane__list");
      scroller.scrollTop = scroller.scrollHeight;
    });
    await this.settle(3);
  }

  async scrollToMiddle(): Promise<void> {
    await this.page.evaluate(() => {
      const scroller = document.querySelector(".timeline-pane__list") as HTMLElement | null;
      if (!scroller) throw new Error("missing .timeline-pane__list");
      scroller.scrollTop = Math.max(0, Math.round((scroller.scrollHeight - scroller.clientHeight) * 0.5));
    });
    await this.settle(3);
  }

  async setViewport(width: number, height: number): Promise<void> {
    await this.page.setViewportSize({ width, height });
    await this.settle(3);
  }

  /** Prepend `count` older events to the start of the projection (without triggering SSE yet). */
  stageOlderPrepend(count: number): void {
    const older = Array.from({ length: count }, (_, i) => {
      const id = this.nextPrependId++;
      const ts = new Date(Date.parse("2026-04-15T14:00:00Z") + id * 1000).toISOString();
      return {
        kind: "event",
        session_id: this.sessionId,
        timestamp: ts,
        event: {
          id,
          role: i % 2 === 0 ? "user" : "assistant",
          content_text: `Older history message #${id} loaded during pagination.`,
          tool_name: null,
          tool_input_json: null,
          tool_output_text: null,
          tool_call_id: null,
          timestamp: ts,
          in_active_context: true,
        },
      };
    });
    this.workspaceState.projection.items = [...older, ...this.workspaceState.projection.items];
    this.workspaceState.projection.page_offset = Math.max(0, (this.workspaceState.projection.page_offset ?? 0) - count);
  }

  async prependOlderPage(count: number): Promise<void> {
    this.stageOlderPrepend(count);
    await this.pushWorkspaceChanged(this.nextEventId++);
  }

  async emitThinkingPreview(text: string): Promise<void> {
    const previewId = this.nextEventId++;
    await this.page.evaluate(
      ({ sessionId, previewId, text }) => {
        (window as unknown as { __emitWorkspaceChanged: (p: unknown) => void }).__emitWorkspaceChanged({
          session_id: sessionId,
          change_kind: "transcript_preview",
          latest_event_id: previewId,
          server_now_ms: Date.now(),
          transcript_preview: {
            event_id: previewId,
            kind: "thinking",
            text,
            event_origin: "live_provisional",
            updated_at: new Date().toISOString(),
          },
        });
      },
      { sessionId: this.sessionId, previewId, text },
    );
    await this.settle(4);
  }

  async promotePreviewToDurable(text: string): Promise<void> {
    const id = this.nextEventId++;
    const ts = new Date(Date.now()).toISOString();
    this.workspaceState.projection.items.push({
      kind: "event",
      session_id: this.sessionId,
      timestamp: ts,
      event: {
        id,
        role: "assistant",
        content_text: text,
        tool_name: null,
        tool_input_json: null,
        tool_output_text: null,
        tool_call_id: null,
        timestamp: ts,
        in_active_context: true,
      },
    });
    this.workspaceState.projection.total = this.workspaceState.projection.items.length;
    await this.page.evaluate(
      ({ sessionId, latestId }) => {
        (window as unknown as { __emitWorkspaceChanged: (p: unknown) => void }).__emitWorkspaceChanged({
          session_id: sessionId,
          change_kind: "ingest",
          latest_event_id: latestId,
          server_now_ms: Date.now(),
          transcript_preview: null,
        });
      },
      { sessionId: this.sessionId, latestId: id },
    );
    await this.page.locator('[data-testid="session-timeline-row"]', { hasText: text.slice(0, 24) }).first().waitFor({ state: "attached", timeout: 5_000 });
    await this.settle(4);
  }

  /** Append a durable assistant prose row so we have a known anchor right above the live tool. */
  async appendAssistantProse(text: string): Promise<void> {
    const id = this.nextEventId++;
    const ts = new Date(Date.now()).toISOString();
    const event = {
      id,
      role: "assistant",
      content_text: text,
      tool_name: null,
      tool_input_json: null,
      tool_output_text: null,
      tool_call_id: null,
      timestamp: ts,
      in_active_context: true,
    };
    this.workspaceState.projection.items.push({
      kind: "event",
      session_id: this.sessionId,
      timestamp: ts,
      event,
    });
    await this.pushWorkspaceChanged(id);
    await this.page.locator('[data-testid="session-timeline-row"]', { hasText: text.slice(0, 24) }).first().waitFor({ state: "attached", timeout: 5_000 });
    await this.settle(3);
  }

  /** Start or update a tool call + result in the workspace and notify the page via SSE. */
  async upsertToolCall(callId: string, command: string, outputText: string | null, state: "running" | "completed"): Promise<void> {
    const items = this.workspaceState.projection.items as Array<{
      kind: string;
      session_id: string;
      timestamp: string;
      event?: Record<string, unknown> | null;
    }>;
    let callEvent = items.find((item) => item.event?.tool_call_id === callId && item.event?.role === "assistant")?.event ?? null;
    if (!callEvent) {
      const id = this.nextEventId++;
      const ts = new Date(Date.now()).toISOString();
      callEvent = {
        id,
        role: "assistant",
        content_text: null,
        tool_name: "Bash",
        tool_input_json: { command },
        tool_output_text: null,
        tool_call_id: callId,
        tool_call_state: state,
        timestamp: ts,
        in_active_context: true,
      };
      items.push({ kind: "event", session_id: this.sessionId, timestamp: ts, event: callEvent });
    } else {
      callEvent.tool_call_state = state;
    }

    let resultId = Number(callEvent.id);
    if (outputText !== null) {
      let resultEvent = items.find((item) => item.event?.tool_call_id === callId && item.event?.role === "tool")?.event ?? null;
      if (!resultEvent) {
        resultId = this.nextEventId++;
        const ts = new Date(Date.now()).toISOString();
        resultEvent = {
          id: resultId,
          role: "tool",
          content_text: null,
          tool_name: "Bash",
          tool_input_json: null,
          tool_output_text: outputText,
          tool_call_id: callId,
          timestamp: ts,
          in_active_context: true,
        };
        items.push({ kind: "event", session_id: this.sessionId, timestamp: ts, event: resultEvent });
      } else {
        resultEvent.tool_output_text = outputText;
      }
    }
    await this.pushWorkspaceChanged(this.nextEventId++);
    if (outputText) {
      const lines = outputText.split("\n");
      const probe = lines[lines.length - 1].slice(0, 28);
      await this.page.locator('[data-testid="tool-output-preview"]', { hasText: probe }).last().waitFor({ state: "attached", timeout: 2_000 }).catch(() => {});
    }
    await this.settle(3);
  }

  private async pushWorkspaceChanged(latestId: number): Promise<void> {
    this.workspaceState.projection.total = this.workspaceState.projection.items.length;
    await this.page.evaluate(
      ({ sessionId, latestId }) => {
        (window as unknown as { __emitWorkspaceChanged: (p: unknown) => void }).__emitWorkspaceChanged({
          session_id: sessionId,
          change_kind: "ingest",
          latest_event_id: latestId,
          server_now_ms: Date.now(),
        });
      },
      { sessionId: this.sessionId, latestId },
    );
    await this.settle(4);
  }

  async resetCls(): Promise<void> {
    await this.page.evaluate(() => (window as unknown as { __sampler: { resetCls: () => void } }).__sampler.resetCls());
  }

  async startSampling(anchorToken: string): Promise<void> {
    await this.page.evaluate((t) => (window as unknown as { __sampler: { start: (t: string) => void } }).__sampler.start(t), anchorToken);
  }

  async stopSampling(): Promise<FrameSample[]> {
    return this.page.evaluate(() => (window as unknown as { __sampler: { stop: () => FrameSample[] } }).__sampler.stop());
  }

  async snap(anchorToken: string): Promise<FrameSample> {
    return this.page.evaluate((t) => (window as unknown as { __sampler: { snap: (t: string) => FrameSample } }).__sampler.snap(t), anchorToken);
  }
}

const STREAM_CHUNKS = [
  "Installed 2 packages in 4ms",
  "Installed 2 packages in 4ms\nsjc.dsp.di.ad-beacon: decoded=20 types={'ca': 10, 'se': 3}",
  "Installed 2 packages in 4ms\nsjc.dsp.di.ad-beacon: decoded=20 types={'ca': 10, 'se': 3, 've': 4, 'sev': 1, 'ar': 1, 'tk': 1} countries=['CN', 'US'] samples=[{'dc': 'sjc', 'event': 'se', 'ts': 1794526459117, 'country': 'US', 'state': 'AZ', 'city': '5308655', 'zip': '85014', 'cpm': 60.0, 'has_camp': True, 'rtb': '{\"rtbServerId\":\"sjc-230\"}'}]",
  Array.from({ length: 12 }, (_, i) => `chunk-${i}: sjc.dsp.di.ad-beacon decoded=20 countries=['CN','US','GB'] payload=${"x".repeat(120)}`).join("\n"),
  Array.from({ length: 24 }, (_, i) => `chunk-${i}: ams.dsp.di.ad-beacon decoded=20 countries=['AT','ES','GH'] payload=${"y".repeat(120)}`).join("\n"),
];

function maxAnchorDrift(frames: FrameSample[], top0: number): number {
  let worst = 0;
  for (const f of frames) {
    if (f.anchorTop === null) return Infinity;
    worst = Math.max(worst, Math.abs(f.anchorTop - top0));
  }
  return worst;
}

function maxSingleFrameAnchorJump(frames: FrameSample[], label?: string): number {
  let worst = 0;
  for (let i = 1; i < frames.length; i++) {
    const p = frames[i - 1];
    const c = frames[i];
    if (p.anchorTop !== null && c.anchorTop !== null) {
      const d = Math.abs(c.anchorTop - p.anchorTop);
      if (label && d > 2) {
        console.log(
          `  [jump ${label} f${i}] dAnchor=${Math.round(c.anchorTop - p.anchorTop)} scrollTop:${Math.round(p.scrollTop)}->${Math.round(c.scrollTop)} scrollH:${Math.round(p.scrollHeight)}->${Math.round(c.scrollHeight)} tail:${p.tail}->${c.tail}`,
        );
      }
      worst = Math.max(worst, d);
    }
  }
  return worst;
}

function countToolWellHeightChanges(frames: FrameSample[]): number {
  let changes = 0;
  let prev: number | null = null;
  for (const f of frames) {
    if (f.toolWellHeight === null) continue;
    if (prev !== null && Math.abs(f.toolWellHeight - prev) > 0.5) {
      changes += 1;
    }
    prev = f.toolWellHeight;
  }
  return changes;
}

function maxToolWellHeight(frames: FrameSample[]): number {
  let max = 0;
  for (const f of frames) {
    if (f.toolWellHeight !== null) max = Math.max(max, f.toolWellHeight);
  }
  return max;
}

const scenarios: Scenario[] = [
  {
    name: "tool_streaming_pinned",
    about: "Pinned at bottom while a Bash tool streams wrapped JSON chunks; tool well holds fixed height while pending.",
    async run(h) {
      const anchor = "STABILITY_ANCHOR_PINNED";
      await h.appendAssistantProse(`${anchor}: Checking the S3 beacon pipeline now.`);
      await h.scrollToBottom();
      await h.upsertToolCall("call-stream-pinned", "bun run decode-beacons", STREAM_CHUNKS[0], "running");
      await h.settle(3);
      await h.resetCls();
      await h.startSampling(anchor);
      for (const chunk of STREAM_CHUNKS.slice(1)) {
        await h.upsertToolCall("call-stream-pinned", "bun run decode-beacons", chunk, "running");
      }
      await h.upsertToolCall("call-stream-pinned", "bun run decode-beacons", STREAM_CHUNKS[4], "completed");
      await h.settle(4);
      const frames = await h.stopSampling();
      const cls = frames.length ? frames[frames.length - 1].clsTotal : 0;
      return [
        le("tool well height jumps", countToolWellHeightChanges(frames), 0, "frames"),
        le("max tool well height", maxToolWellHeight(frames), 125, "px"),
        le("prose max frame jump", maxSingleFrameAnchorJump(frames), 125, "px"),
        le("layout-shift CLS", cls, 0.1, "cls"),
      ];
    },
  },
  {
    name: "tool_streaming_scrolled_up",
    about: "Scrolled up 300px reading prose while a Bash tool streams chunks and completes below.",
    async run(h) {
      const anchor = "STABILITY_ANCHOR_UP";
      await h.appendAssistantProse(`${anchor}: Reading earlier architectural notes while background work runs.`);
      await h.scrollUpBy(300);
      const snap0 = await h.snap(anchor);
      await h.resetCls();
      await h.startSampling(anchor);
      for (const chunk of STREAM_CHUNKS) {
        await h.upsertToolCall("call-stream-up", "bun run decode-beacons", chunk, "running");
      }
      await h.upsertToolCall("call-stream-up", "bun run decode-beacons", STREAM_CHUNKS[4], "completed");
      await h.settle(4);
      const frames = await h.stopSampling();
      const cls = frames.length ? frames[frames.length - 1].clsTotal : 0;
      return [
        le("anchor drift", maxAnchorDrift(frames, snap0.anchorTop!), 1, "px"),
        le("layout-shift CLS", cls, 0.001, "cls"),
      ];
    },
  },
  {
    name: "tool_scrolled_up_slightly",
    about: "Scrolled up 40px above bottom while a tool streams; must not yank reader back to bottom.",
    async run(h) {
      const anchor = "STABILITY_ANCHOR_SLIGHT";
      await h.appendAssistantProse(`${anchor}: Re-reading the last two lines right above the active tool.`);
      await h.upsertToolCall("call-slight", "bun run decode-beacons", STREAM_CHUNKS[0], "running");
      await h.scrollUpBy(40);
      const snap0 = await h.snap(anchor);
      await h.resetCls();
      await h.startSampling(anchor);
      for (const chunk of STREAM_CHUNKS.slice(1)) {
        await h.upsertToolCall("call-slight", "bun run decode-beacons", chunk, "running");
      }
      await h.settle(4);
      const frames = await h.stopSampling();
      return [le("anchor drift", maxAnchorDrift(frames, snap0.anchorTop!), 1, "px")];
    },
  },
  {
    name: "tool_collapses_into_trail_scrolled_up",
    about: "Scrolled up while a completed Bash tool collapses into a .tl-trail row as new prose arrives.",
    async run(h) {
      const anchor = "STABILITY_ANCHOR_TRAIL_UP";
      await h.appendAssistantProse(`${anchor}: Earlier paragraph that must stay stationary.`);
      await h.upsertToolCall("call-trail-up", "bun run decode-beacons", STREAM_CHUNKS[4], "completed");
      await h.scrollUpBy(250);
      const snap0 = await h.snap(anchor);
      await h.resetCls();
      await h.startSampling(anchor);
      await h.appendAssistantProse("New assistant summary after the tool finished.");
      await h.settle(4);
      const frames = await h.stopSampling();
      return [le("anchor drift", maxAnchorDrift(frames, snap0.anchorTop!), 1, "px")];
    },
  },
  {
    name: "tool_collapses_into_trail_pinned",
    about: "Pinned at bottom when completed Bash tool collapses into .tl-trail as new prose arrives (diagnostic).",
    async run(h) {
      const anchor = "STABILITY_ANCHOR_TRAIL_PIN";
      await h.appendAssistantProse(`${anchor}: Prose right above the completed tool.`);
      await h.upsertToolCall("call-trail-pin", "bun run decode-beacons", STREAM_CHUNKS[4], "completed");
      await h.scrollToBottom();
      await h.resetCls();
      await h.startSampling(anchor);
      await h.appendAssistantProse("New assistant summary after the tool finished.");
      await h.settle(4);
      const frames = await h.stopSampling();
      const cls = frames.length ? frames[frames.length - 1].clsTotal : 0;
      return [
        le("prose max frame jump", maxSingleFrameAnchorJump(frames), 120, "px", true),
        le("layout-shift CLS", cls, 0.25, "cls", true),
      ];
    },
  },
  {
    name: "prepend_mid_scroll",
    about: "Reader is mid-transcript (~50% scroll) when an older page prepends; overflow-anchor: none prevents double compensation.",
    async run(h) {
      const anchor = "STABILITY_ANCHOR_PREPEND_MID";
      await h.appendAssistantProse(`${anchor}: Visible paragraph while scrolled to mid-transcript.`);
      await h.scrollToMiddle();
      const snap0 = await h.snap(anchor);
      await h.resetCls();
      await h.startSampling(anchor);
      await h.prependOlderPage(6);
      await h.settle(4);
      const frames = await h.stopSampling();
      const cls = frames.length ? frames[frames.length - 1].clsTotal : 0;
      return [
        le("anchor drift", maxAnchorDrift(frames, snap0.anchorTop!), 2, "px"),
        le("layout-shift CLS", cls, 0.005, "cls"),
      ];
    },
  },
  {
    name: "prepend_plus_preview_promote",
    about: "Older page prepends in the same update that promotes a trailing live preview to a durable row.",
    async run(h) {
      const anchor = "STABILITY_ANCHOR_PREPEND_PROMOTE";
      await h.appendAssistantProse(`${anchor}: Paragraph visible mid-transcript before mixed prepend + promote.`);
      await h.emitThinkingPreview("Live thinking preview that will be replaced by a durable row...");
      await h.scrollToMiddle();
      const snap0 = await h.snap(anchor);
      await h.resetCls();
      await h.startSampling(anchor);
      h.stageOlderPrepend(6);
      await h.promotePreviewToDurable("Durable assistant response after preview promotion.");
      const frames = await h.stopSampling();
      return [le("anchor drift", maxAnchorDrift(frames, snap0.anchorTop!), 2, "px", true)];
    },
  },
  {
    name: "preview_promote_pinned_tall_viewport",
    about: "Tall viewport (~1400px), pinned at bottom; live preview promotes to durable without placeholder bounce.",
    async run(h) {
      await h.setViewport(1440, 1400);
      const anchor = "STABILITY_ANCHOR_TALL";
      await h.appendAssistantProse(`${anchor}: Reading near the bottom on a tall 1400px viewport.`);
      await h.scrollToBottom();
      await h.emitThinkingPreview("Thinking line 1\nThinking line 2\nThinking line 3\nThinking line 4\nThinking line 5");
      await h.resetCls();
      await h.startSampling(anchor);
      await h.promotePreviewToDurable("Concise final durable answer.");
      const frames = await h.stopSampling();
      const cls = frames.length ? frames[frames.length - 1].clsTotal : 0;
      return [
        le("prose max frame jump", maxSingleFrameAnchorJump(frames), 120, "px"),
        le("layout-shift CLS", cls, 0.1, "cls"),
      ];
    },
  },
];

const stopFrontend = await ensureFrontend(BASE_URL, { handleSignals: false });
const browser = await chromium.launch({ args: ["--enable-unsafe-swiftshader"] });
const results: Array<{ name: string; about: string; checks: Check[]; error?: string }> = [];

try {
  for (const scenario of scenarios) {
    if (only && !only.includes(scenario.name)) continue;
    const h = new WebStabilityHarness();
    try {
      await h.open(browser);
      const checks = await scenario.run(h);
      results.push({ name: scenario.name, about: scenario.about, checks });
    } catch (error) {
      results.push({ name: scenario.name, about: scenario.about, checks: [], error: String((error as Error).stack ?? error) });
    } finally {
      await h.close();
    }
  }
} finally {
  await browser.close();
  await stopFrontend();
}

let failed = 0;
for (const r of results) {
  if (r.error) {
    failed += 1;
    console.log(`ERROR ${r.name}\n  ${r.error.split("\n").slice(0, 4).join("\n  ")}`);
    continue;
  }
  for (const c of r.checks) {
    const status = c.pass ? "ok  " : c.advisory ? "warn" : "FAIL";
    if (!c.pass && !c.advisory) failed += 1;
    console.log(`${status} ${r.name.padEnd(36)} ${c.name.padEnd(26)} ${String(c.value).padStart(8)} ${c.unit.padEnd(6)} (budget <= ${c.budget})`);
  }
}

writeFileSync(path.join(outDir, "results.json"), JSON.stringify({ results, failed }, null, 2));
console.log(`Results saved to ${path.join(outDir, "results.json")}`);
process.exit(failed > 0 ? 1 : 0);
