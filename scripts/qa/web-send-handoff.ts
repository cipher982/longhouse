#!/usr/bin/env bun
/**
 * Web send-handoff check: does a message the user sends stay on screen, in
 * one place and one shape, from the click until the transcript owns it?
 *
 * Drives the real SessionDetailPage in headless Chromium against a scripted
 * server (Playwright routes plus a mock workspace stream). Each scenario is
 * an ordering of server facts seen on real hosts: the POST answer, the
 * receipt's transcript link, a live thinking preview, and the ingest that
 * brings the user's row into the projection. Every animation frame records
 * each element that shows the sent text: what it is (outbox row or
 * transcript row), its header, its box, and a stable per-node identity.
 *
 *   bun scripts/qa/web-send-handoff.ts [--only=name,name] [--out=dir]
 *
 * Checks per scenario, all measured from the first frame that shows the text:
 *   gap frames        frames with no element showing the text       (budget 0)
 *   duplicate frames  frames with two or more                       (budget 0)
 *   remounts          new DOM nodes after the first one             (budget 0)
 *   shape changes     header changes other than the delivery status,
 *                     e.g. a time or origin label appearing late   (budget 0)
 *   max jump          largest frame-to-frame move of the row within
 *                     the transcript (scrolling is not a move)      (budget 2 px)
 *
 * Output: results.json, frames-<scenario>.json, and a PNG strip of each
 * distinct frame state under --out (default /tmp/agents/web-send-handoff/<stamp>).
 */
import "../playwright-store-auto.mjs";
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { chromium, type Browser, type BrowserContext, type Page, type Route } from "playwright";
import { ensureFrontend } from "../ui/frontend";
import {
  buildRailSessionsFixture,
  buildSessionProseIdleFixture,
  makeEvent,
  projectionEvent,
} from "../ui-fixtures/sessionDetailStress";

const arg = (name: string) => process.argv.find((a) => a.startsWith(`--${name}=`))?.split("=")[1];
const only = arg("only")?.split(",").filter(Boolean);
const stamp = new Date().toISOString().replace(/[-:]/g, "").slice(0, 15);
const outDir = arg("out") ?? path.join("/tmp/agents/web-send-handoff", stamp);
mkdirSync(outDir, { recursive: true });

const PORT = Number(process.env.WEB_STABILITY_PORT ?? 47299);
const BASE_URL = process.env.FRONTEND_URL ?? `http://127.0.0.1:${PORT}`;
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

const MESSAGE =
  '"Keep it correct for Claude." ok but make sure we arent tilting into just patching one provider. HANDOFF_TOKEN';
const TOKEN = "HANDOFF_TOKEN";

const INIT_SCRIPT = `
(() => {
  const NativeEventSource = window.EventSource;
  const instances = [];
  class MockEventSource {
    constructor(url, opts) {
      this.url = String(url);
      this.withCredentials = Boolean(opts && opts.withCredentials);
      this.readyState = 1;
      this.listeners = new Map();
      instances.push(this);
      setTimeout(() => this._dispatch("connected", { session_id: "x", server_now_ms: Date.now() }), 10);
    }
    addEventListener(type, fn) {
      const set = this.listeners.get(type) || new Set();
      set.add(fn);
      this.listeners.set(type, set);
    }
    removeEventListener(type, fn) { this.listeners.get(type)?.delete(fn); }
    close() { this.readyState = 2; }
    _dispatch(type, payload) {
      const ev = new MessageEvent(type, { data: JSON.stringify(payload) });
      for (const fn of this.listeners.get(type) || []) fn(ev);
    }
  }
  window.EventSource = function (url, opts) {
    return String(url).includes("/workspace/stream") ? new MockEventSource(url, opts) : new NativeEventSource(url, opts);
  };
  window.__emitWorkspaceChanged = (payload) => {
    for (const inst of instances) if (inst.readyState === 1) inst._dispatch("workspace_changed", payload);
  };
  window.__emitStreamEvent = (type, payload) => {
    for (const inst of instances) if (inst.readyState === 1) inst._dispatch(type, payload);
  };

  // Every element that shows the token, every frame. A WeakMap gives each DOM
  // node a stable id, so a remount (new key, new component) is a new id even
  // when it looks the same.
  const ids = new WeakMap();
  let nextId = 1;
  let token = null;
  let frames = [];
  let sampling = false;
  let t0 = 0;
  // Rows that showed the text before the send (an earlier identical message)
  // are not this send. Their nodes are remembered; a remount of one of them
  // would show up as a new node.
  let baseline = new Set();
  let ingestAt = null;
  const label = (el) => (el ? el.textContent.replace(/\\s+/g, " ").trim() : "");
  // The header without its delivery status and actions: the status word is
  // meant to change ("Sending…", "Sent", then gone); the rest is the row's shape.
  const shape = (el) => {
    if (!el) return "";
    const copy = el.cloneNode(true);
    copy.querySelectorAll("[role=status], button, time").forEach((node) => node.remove());
    return label(copy);
  };
  function read() {
    const scroller = document.querySelector(".timeline-pane__list");
    const scrollerTop = scroller ? scroller.getBoundingClientRect().top : 0;
    const rows = Array.from(document.querySelectorAll(".timeline-pane__rows > *"));
    const shown = [];
    rows.forEach((el, index) => {
      if (!token || !(el.textContent || "").includes(token)) return;
      if (baseline.has(el)) return;
      if (!ids.has(el)) ids.set(el, nextId++);
      const r = el.getBoundingClientRect();
      shown.push({
        node: ids.get(el),
        kind: el.matches("[data-testid=session-outbox-row]") ? "outbox:" + el.getAttribute("data-outbox-state") : "transcript",
        head: shape(el.querySelector(".tl-msg__head")) + " | " + shape(el.querySelector(".tl-msg__meta")),
        status: label(el.querySelector(".tl-msg__meta [role=status], .tl-msg__head [role=status]")),
        fromEnd: rows.length - 1 - index,
        top: Math.round(r.top),
        // Position within the transcript: moves only when rows above it change.
        docTop: Math.round(r.top - scrollerTop + (scroller ? scroller.scrollTop : 0)),
        height: Math.round(r.height),
      });
    });
    const box = document.querySelector(".session-chat-composer textarea");
    return {
      t: Math.round(performance.now() - t0),
      composerEmpty: box ? box.value.trim() === "" : false,
      ingestAt,
      scrollTop: scroller ? Math.round(scroller.scrollTop) : 0,
      scrollGap: scroller ? Math.round(scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop) : 0,
      tail: rows.slice(-3).map((el) => el.getAttribute("data-row-kind") || el.getAttribute("data-testid") || el.className.split(" ")[0]).join(","),
      shown,
    };
  }
  function tick() {
    if (!sampling) return;
    frames.push(read());
    requestAnimationFrame(tick);
  }
  window.__handoff = {
    start(t) {
      token = t;
      frames = [];
      ingestAt = null;
      baseline = new Set(
        Array.from(document.querySelectorAll(".timeline-pane__rows > *")).filter((el) => (el.textContent || "").includes(t)),
      );
      t0 = performance.now();
      sampling = true;
      requestAnimationFrame(tick);
    },
    markIngest() { ingestAt = Math.round(performance.now() - t0); },
    stop() { sampling = false; frames.push(read()); return frames; },
  };
})();
`;

interface Shown {
  node: number;
  kind: string;
  head: string;
  status: string;
  docTop: number;
  fromEnd: number;
  top: number;
  height: number;
}
interface Frame {
  t: number;
  composerEmpty: boolean;
  ingestAt: number | null;
  scrollTop: number;
  scrollGap: number;
  tail: string;
  shown: Shown[];
}
interface Check {
  name: string;
  value: number;
  budget: number;
  unit: string;
  pass: boolean;
}

/** What the scripted server currently says about the one input. */
interface ServerState {
  receipt: Record<string, unknown> | null;
  projectionHasUserRow: boolean;
}

/**
 * One ordering of the server facts that follow a send, in ms after the POST
 * answers. Omitted facts never happen.
 */
interface Scenario {
  name: string;
  about: string;
  /** Receipt status in the POST answer: delivered already, or still delivering. */
  postStatus: "delivered" | "delivering";
  deliverAt?: number;
  /** The linker ties the receipt to its transcript event (durable_event_id). */
  linkAt?: number;
  /** The reply's live thinking preview arrives on the stream. */
  thinkingAt?: number;
  /** The user's row enters the projection and the stream says "ingest". */
  ingestAt: number;
  /** The ingested row carries input_origin (client_request_id). */
  ingestOrigin: boolean;
  /** Server time to answer a workspace/projection read. */
  fetchLatencyMs: number;
  /**
   * After an ingest wake the server pushes the newest rows (a projection_delta
   * frame, read in fetchLatencyMs), as the real stream does for the web
   * ("push", the default). "lost": the wake promises them but they never
   * come, so the client reads them over HTTP after its wait. "none": an
   * older server with no push.
   */
  push?: "push" | "lost" | "none";
  /** The same words were typed in the terminal a few seconds before the send. */
  repeatOfTypedRow?: boolean;
}

// Rows a pushed delta carries, as the server's PROJECTION_DELTA_WINDOW.
const PUSH_WINDOW = 40;
type Projection = { items: Array<{ event?: { id: number; cursor?: string } | null }>; total?: number } & Record<string, unknown>;

class Harness {
  context!: BrowserContext;
  page!: Page;
  fixture = buildSessionProseIdleFixture();
  workspace = structuredClone(this.fixture.workspace) as { projection: Projection } & Record<string, unknown>;
  sessionId = this.fixture.session.id as string;
  state: ServerState = { receipt: null, projectionHasUserRow: false };
  clientRequestId: string | null = null;
  postedAt = 0;
  nextEventId = 990_000;
  log: Array<{ t: number; what: string }> = [];
  requested: string[] = [];
  dataRequested = (() => {
    let resolve!: () => void;
    const promise = new Promise<void>((r) => (resolve = r));
    return { promise, resolve };
  })();

  note(what: string) {
    this.log.push({ t: this.postedAt ? Date.now() - this.postedAt : -1, what });
  }

  json(route: Route, body: unknown) {
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  }

  async open(browser: Browser, scenario: Scenario): Promise<void> {
    // A storage-v2 projection: a generation and a cursor per event, so the
    // client's delta reads and the pushed deltas both apply.
    const projection = this.workspace.projection;
    projection.generation_id = "gen-qa";
    projection.head_session_id = projection.head_session_id ?? this.sessionId;
    projection.has_more = false;
    projection.next_cursor = null;
    if (scenario.repeatOfTypedRow) {
      const ts = new Date(Date.now() - 3_000).toISOString();
      projection.items.push(projectionEvent(makeEvent(this.nextEventId++, "user", ts, { content_text: MESSAGE } as never), this.sessionId) as never);
    }
    for (const item of projection.items) if (item.event) item.event.cursor = `c-${item.event.id}`;
    this.context = await browser.newContext({ viewport: { width: 1440, height: 900 }, reducedMotion: "reduce" });
    await this.context.addInitScript(INIT_SCRIPT);
    const base = `/api/timeline/sessions/${this.sessionId}`;
    await this.context.route(`${BASE_URL}/api/**`, async (route) => {
      const url = new URL(route.request().url());
      const p = url.pathname;
      const method = route.request().method();
      this.requested.push(`${method} ${p.replace(this.sessionId, ":id")}`);
      if (p === `${base}/workspace` || p === `${base}/projection`) this.dataRequested.resolve();
      if (p === base) return this.json(route, this.fixture.session);
      if (p === `${base}/thread`) return this.json(route, this.fixture.thread);
      if (p === `${base}/workspace` || p === `${base}/projection`) {
        // The server reads its state when the request arrives and answers later.
        const body = structuredClone(p.endsWith("/workspace") ? this.workspace : this.workspace.projection) as Record<string, unknown>;
        const cursor = url.searchParams.get("cursor");
        if (p.endsWith("/projection") && cursor) {
          const items = (body.items as Projection["items"]) ?? [];
          const at = items.findIndex((item) => item.event?.cursor === cursor);
          body.items = url.searchParams.get("anchor") === "tail" ? (at < 0 ? [] : items.slice(0, at)) : items.slice(at + 1);
          body.has_more = false;
        }
        if (p.endsWith("/projection")) this.note(cursor ? "GET projection delta" : "GET projection");
        await sleep(scenario.fetchLatencyMs);
        return this.json(route, body);
      }
      if (p === `${base}/turns`) return this.json(route, this.fixture.turns);
      if (p === `${base}/subagents`) return this.json(route, { subagents: [] });
      if (p === `/api/sessions/${this.sessionId}/lock`) return this.json(route, { locked: false });
      if (p === `/api/sessions/${this.sessionId}/input` && method === "POST") {
        const body = JSON.parse(route.request().postData() ?? "{}");
        this.clientRequestId = body.client_request_id;
        const answer = postAnswer(body.client_request_id, scenario.postStatus);
        this.state.receipt = answer.receipt;
        this.postedAt = Date.now();
        this.note(`POST answered (${scenario.postStatus})`);
        return this.json(route, answer.body);
      }
      if (p === `/api/sessions/${this.sessionId}/inputs`) {
        const wanted = url.searchParams.get("client_request_id");
        const rows = this.state.receipt ? [this.state.receipt] : [];
        this.note(wanted ? "GET receipt (exact)" : "GET receipts (list)");
        return this.json(route, wanted ? rows.filter((r) => r.client_request_id === wanted) : rows);
      }
      if (p === "/api/timeline/sessions") return this.json(route, buildRailSessionsFixture(this.fixture.session));
      if (p === "/api/auth/status") {
        return this.json(route, { authenticated: true, user: { id: 1, email: "qa@longhouse.ai", display_name: "QA", role: "admin" } });
      }
      if (p === "/api/auth/methods") return this.json(route, { google: false, password: false, sso: false, sso_url: null });
      if (p === "/api/auth/me") return this.json(route, { id: 1, email: "qa@longhouse.ai", name: "QA", role: "admin" });
      return this.json(route, {});
    });
    this.page = await this.context.newPage();
    this.page.on("pageerror", (err) => console.error("[pageerror]", err.message));
    // Setup, not the measurement: wait on the page's own signals, in order, so a
    // starved CI runner shows as a slow boot and a real render failure still fails.
    // 1. The app's module graph loaded (Vite transforms on demand, and a loaded
    //    runner can take longer than any fixed render budget to get here).
    await this.page.goto(`${BASE_URL}/timeline/${this.sessionId}`, { waitUntil: "load", timeout: 90_000 });
    // 2. The app asked for the session; only then is a missing row the app's fault.
    const stalled = async (stage: string) =>
      new Error(`${stage}; the app had requested: [${this.requested.join(", ")}]; page url ${this.page.url()}`);
    await Promise.race([
      this.dataRequested.promise,
      sleep(90_000).then(() => Promise.reject(undefined)),
    ]).catch(async () => {
      throw await stalled("the app never requested the session (it did not boot)");
    });
    // 3. Rendering the answer is the app's own work and keeps a tight budget.
    await this.page.locator('[data-testid="session-timeline-row"]').first().waitFor({ state: "visible", timeout: 15_000 })
      .catch(async (e) => { throw await stalled(`the session was requested but no row rendered (${(e as Error).message.split("\n")[0]})`); });
    await this.page.locator(".session-chat-composer textarea").waitFor({ state: "visible", timeout: 15_000 });
    await sleep(500);
  }

  async emit(payload: Record<string, unknown>) {
    await this.page.evaluate(
      (p) => (window as unknown as { __emitWorkspaceChanged: (p: unknown) => void }).__emitWorkspaceChanged(p),
      { session_id: this.sessionId, server_now_ms: Date.now(), ...payload },
    );
  }

  /** Server: a live thinking preview for the reply the send started. */
  async thinking(text: string) {
    const id = this.nextEventId++;
    await this.emit({
      change_kind: "transcript_preview",
      latest_event_id: id,
      transcript_preview: { event_id: id, kind: "thinking", text, event_origin: "live_provisional", timestamp: new Date().toISOString(), updated_at: new Date().toISOString() },
    });
  }

  /** Server: the provider took the input. */
  deliver() {
    if (this.state.receipt) this.state.receipt = { ...this.state.receipt, status: "delivered", delivery_status: "delivered" };
  }

  /** Server: the linker ties the receipt to its transcript event. */
  link() {
    if (this.state.receipt) this.state.receipt = { ...this.state.receipt, durable_event_id: "evt-user-uuid" };
  }

  /** Server: ingest puts the user's row in the projection and wakes the stream. */
  fetchLatencyMs = 0;

  async ingestUserRow(withOrigin: boolean, mode: "push" | "lost" | "none") {
    const id = this.nextEventId++;
    const ts = new Date(this.postedAt).toISOString();
    const event = makeEvent(id, "user", ts, {
      content_text: MESSAGE,
      ...(withOrigin
        ? { input_origin: { authored_via: "longhouse", origin: "user", client_request_id: this.clientRequestId, session_input_id: null } }
        : {}),
    } as never);
    (event as { cursor?: string }).cursor = `c-${id}`;
    this.workspace.projection.items.push(projectionEvent(event, this.sessionId) as never);
    this.workspace.projection.total = this.workspace.projection.items.length;
    this.state.projectionHasUserRow = true;
    await this.page.evaluate(() => (window as unknown as { __handoff: { markIngest: () => void } }).__handoff.markIngest());
    await this.emit({ change_kind: "ingest", latest_event_id: id, transcript_preview: null, ...(mode !== "none" ? { projection_delta: "follows" } : {}) });
    if (mode !== "push") return;
    // The server reads the newest rows beside the frame loop and pushes them.
    const snapshot = structuredClone(this.workspace.projection);
    setTimeout(() => {
      const items = snapshot.items;
      const tail = items.slice(-PUSH_WINDOW);
      const anchorAt = tail.findIndex((item) => item.event?.cursor);
      if (anchorAt < 0) return;
      const delta = {
        ...snapshot,
        anchor_cursor: tail[anchorAt].event!.cursor,
        items: tail.slice(anchorAt + 1),
        has_more: false,
        next_cursor: null,
      };
      this.note("projection_delta pushed");
      void this.page
        .evaluate(
          (payload) => (window as unknown as { __emitStreamEvent: (t: string, p: unknown) => void }).__emitStreamEvent("projection_delta", payload),
          { session_id: this.sessionId, delta },
        )
        .catch(() => {});
    }, this.fetchLatencyMs);
  }

  async send() {
    await this.page.locator(".session-chat-composer textarea").fill(MESSAGE);
    await this.page.evaluate((t) => (window as unknown as { __handoff: { start: (t: string) => void } }).__handoff.start(t), TOKEN);
    await this.page.locator(".session-chat-composer textarea").press("Enter");
  }

  async stop(): Promise<Frame[]> {
    return this.page.evaluate(() => (window as unknown as { __handoff: { stop: () => Frame[] } }).__handoff.stop());
  }

  async close() {
    await this.context?.close();
  }
}

function postAnswer(clientRequestId: string, status: "delivered" | "delivering") {
  const receipt: Record<string, unknown> = {
    id: null,
    live_input_id: "live-input-1",
    client_request_id: clientRequestId,
    durable_event_id: null,
    origin: "user",
    text: MESSAGE,
    intent: "auto",
    status,
    disposition: "accepted",
    delivery_status: status,
    last_error: null,
    created_at: new Date().toISOString(),
    attachments: [],
    turn: null,
  };
  return {
    receipt,
    body: {
      disposition: "accepted",
      outcome: status === "delivered" ? "sent" : "unknown",
      input_id: null,
      live_input_id: "live-input-1",
      client_request_id: clientRequestId,
      intent: "auto",
      turn: null,
      // The real POST answers with the session's recent receipts, this one included.
      queued: [receipt],
    },
  };
}

function steps(s: Scenario): Array<{ at: number; what: string; apply(h: Harness): Promise<void> | void }> {
  const out: Array<{ at: number; what: string; apply(h: Harness): Promise<void> | void }> = [];
  if (s.deliverAt != null) out.push({ at: s.deliverAt, what: "receipt delivered", apply: (h) => h.deliver() });
  if (s.linkAt != null) out.push({ at: s.linkAt, what: "receipt linked to transcript event", apply: (h) => h.link() });
  if (s.thinkingAt != null) out.push({ at: s.thinkingAt, what: "thinking preview", apply: (h) => h.thinking("Checking how the turn list reads user rows…") });
  out.push({ at: s.ingestAt, what: `user row ingested${s.ingestOrigin ? "" : " (no origin)"}`, apply: (h) => h.ingestUserRow(s.ingestOrigin, s.push ?? "push") });
  return out.sort((a, b) => a.at - b.at);
}

const named: Scenario[] = [
  {
    name: "helm_thinking_before_ingest",
    about:
      "david010 2026-11-15 01:55 (session 60e0cd2a): Claude Helm send. Receipt delivered and linked within a second; the reply's thinking streams before the user's row is ingested.",
    postStatus: "delivered",
    linkAt: 300,
    thinkingAt: 400,
    ingestAt: 1600,
    ingestOrigin: true,
    fetchLatencyMs: 150,
  },
  {
    name: "delivering_then_linked_slow_read",
    about: "POST answers while delivery is in flight; delivered and linked at 900 ms; the ingest wake lands at 1500 ms but the projection read takes 900 ms.",
    postStatus: "delivering",
    deliverAt: 900,
    linkAt: 900,
    thinkingAt: 1000,
    ingestAt: 1500,
    ingestOrigin: true,
    fetchLatencyMs: 900,
  },
  {
    name: "ingest_before_link",
    about: "The user's row is ingested before the receipt is linked.",
    postStatus: "delivered",
    linkAt: 900,
    thinkingAt: 1100,
    ingestAt: 500,
    ingestOrigin: true,
    fetchLatencyMs: 150,
  },
  {
    name: "unlinked_row_no_origin",
    about: "Receipt delivered, never linked; the ingested row has no input_origin, so the client matches it by text.",
    postStatus: "delivered",
    thinkingAt: 400,
    ingestAt: 1600,
    ingestOrigin: false,
    fetchLatencyMs: 150,
  },
  {
    name: "repeated_text_after_typed_row",
    about:
      "The same words ('yes') were typed in the terminal 3 s earlier. The new send's echo has no origin; the older row must not be taken for it.",
    postStatus: "delivered",
    thinkingAt: 400,
    ingestAt: 1600,
    ingestOrigin: false,
    fetchLatencyMs: 150,
    repeatOfTypedRow: true,
  },
  {
    name: "push_lost_http_fallback",
    about: "The server marks the ingest wake 'projection_delta follows' but the pushed rows never arrive; the client reads them over HTTP after its wait.",
    postStatus: "delivered",
    linkAt: 300,
    thinkingAt: 400,
    ingestAt: 1600,
    ingestOrigin: true,
    fetchLatencyMs: 150,
    push: "lost",
  },
];

// --sweep: every combination of when each fact lands. The rule must hold for
// all of them, not only the orderings someone thought to write down.
function sweep(): Scenario[] {
  const out: Scenario[] = [];
  for (const postStatus of ["delivered", "delivering"] as const)
    for (const linkAt of [300, 1200, undefined])
      for (const thinkingAt of [400, undefined])
        for (const ingestAt of [150, 900, 2600])
          for (const fetchLatencyMs of [50, 800])
            for (const ingestOrigin of [true, false]) {
            const deliverAt = postStatus === "delivering" ? Math.min(linkAt ?? 700, 700) : undefined;
            out.push({
              name: `sweep_${postStatus}_link${linkAt ?? "never"}_think${thinkingAt ?? "none"}_ingest${ingestAt}_read${fetchLatencyMs}_${ingestOrigin ? "origin" : "noorigin"}`,
              about: "generated ordering",
              postStatus,
              deliverAt,
              linkAt,
              thinkingAt,
              ingestAt,
              ingestOrigin,
              fetchLatencyMs,
              // Rotates through push, lost push and no push.
              push: (["push", "lost", "none"] as const)[out.length % 3],
            });
          }
  return out;
}
const scenarios = process.argv.includes("--sweep") ? sweep() : named;

function le(name: string, value: number, budget: number, unit: string): Check {
  return { name, value, budget, unit, pass: value <= budget };
}

function judge(frames: Frame[]): { checks: Check[]; story: string[] } {
  const first = frames.findIndex((f) => f.shown.length > 0);
  const tail = first < 0 ? [] : frames.slice(first);
  let gaps = 0;
  let dups = 0;
  let jump = 0;
  const nodes = new Set<number>();
  const heads: string[] = [];
  const story: string[] = [];
  let lastSig = "";
  let prevTop: number | null = null;
  // The composer clears in the same frame the row appears; a frame with an
  // empty composer and no row is the message vanishing for a moment.
  const cleared = frames.findIndex((f) => f.composerEmpty);
  const blank = cleared < 0 ? 0 : frames.slice(cleared, first < 0 ? undefined : first).length;
  // Advisory: ingest to the transcript row showing (the row's head names the
  // ask time, not the pending status word).
  const ingestAt = frames.findLast((f) => f.ingestAt != null)?.ingestAt ?? null;
  const echoed = ingestAt == null ? undefined : frames.find((f) => f.t >= ingestAt && f.shown.length === 1 && !f.shown[0].status);
  const echoMs = ingestAt == null || !echoed ? null : echoed.t - ingestAt;
  for (const f of tail) {
    if (f.shown.length === 0) gaps += 1;
    if (f.shown.length > 1) dups += 1;
    for (const s of f.shown) nodes.add(s.node);
    const one = f.shown[0];
    if (one) {
      const head = one.head.replace(/\d{1,2}:\d{2}( [AP]M)?/, "<time>");
      if (heads.at(-1) !== head) heads.push(head);
      if (prevTop !== null && f.shown.length === 1) jump = Math.max(jump, Math.abs(one.docTop - prevTop));
      prevTop = f.shown.length === 1 ? one.docTop : prevTop;
    }
    const sig = f.shown.length === 0 ? "(nothing)" : f.shown.map((s) => `#${s.node} ${s.kind} "${s.head}"${s.status ? ` [${s.status}]` : ""} y=${s.docTop} h=${s.height} fromEnd=${s.fromEnd}`).join(" + ");
    if (sig !== lastSig) story.push(`${String(f.t).padStart(5)}ms  ${sig}   [tail ${f.tail}]`);
    lastSig = sig;
  }
  if (first < 0) story.push("the sent text never appeared");
  story.push(`echo: ${echoMs == null ? "not seen" : `${echoMs} ms from ingest to transcript row`} (advisory)`);
  return {
    checks: [
      le("appeared", first < 0 ? 1 : 0, 0, "bool"),
      le("cleared, no row", blank, 0, "frames"),
      le("gap frames", gaps, 0, "frames"),
      le("duplicate frames", dups, 0, "frames"),
      le("remounts", Math.max(0, nodes.size - 1), 0, "nodes"),
      le("shape changes", Math.max(0, heads.length - 1), 0, "labels"),
      le("max row jump", jump, 2, "px"),
    ],
    story,
  };
}

const stopFrontend = await ensureFrontend(BASE_URL, { handleSignals: false });
let browser: Browser | null = null;
const results: Array<{ name: string; about: string; checks: Check[]; story: string[]; server: unknown[]; error?: string }> = [];
try {
  // PLAYWRIGHT_CHROMIUM_EXECUTABLE: use an already-installed Chromium when the
  // pinned one is not downloaded.
  browser = await chromium.launch({
    args: ["--enable-unsafe-swiftshader"],
    executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined,
  });
  for (const scenario of scenarios) {
    if (only && !only.includes(scenario.name)) continue;
    const h = new Harness();
    try {
      h.fetchLatencyMs = scenario.fetchLatencyMs;
      await h.open(browser, scenario);
      await h.send();
      const deadline = Date.now() + 5_000;
      while (!h.postedAt && Date.now() < deadline) await sleep(5);
      if (!h.postedAt) throw new Error("the send never reached POST /input");
      if (!process.argv.includes("--sweep")) {
        // A frame of the send before the transcript has it, to look at.
        await sleep(120);
        await h.page.screenshot({ path: path.join(outDir, `${scenario.name}-pending.png`) });
      }
      for (const step of steps(scenario)) {
        const wait = h.postedAt + step.at - Date.now();
        if (wait > 0) await sleep(wait);
        h.note(step.what);
        await step.apply(h);
      }
      await sleep(3_000);
      const frames = await h.stop();
      await h.page.screenshot({ path: path.join(outDir, `${scenario.name}-final.png`) });
      writeFileSync(path.join(outDir, `frames-${scenario.name}.json`), JSON.stringify(frames));
      const { checks, story } = judge(frames);
      results.push({ name: scenario.name, about: scenario.about, checks, story, server: h.log });
    } catch (error) {
      results.push({ name: scenario.name, about: scenario.about, checks: [], story: [], server: h.log, error: String((error as Error).stack ?? error) });
    } finally {
      await h.close();
    }
  }
} finally {
  await browser?.close();
  await stopFrontend();
}

let failed = 0;
for (const r of results) {
  console.log(`\n== ${r.name}\n   ${r.about}`);
  if (r.error) {
    failed += 1;
    console.log(`   ERROR ${r.error.split("\n").slice(0, 4).join("\n   ")}`);
    continue;
  }
  console.log("   server:", r.server.map((s) => `${(s as { t: number }).t}ms ${(s as { what: string }).what}`).join(" | "));
  console.log("   what the screen showed:");
  for (const line of r.story) console.log(`     ${line}`);
  for (const c of r.checks) {
    if (!c.pass) failed += 1;
    console.log(`   ${c.pass ? "ok  " : "FAIL"} ${c.name.padEnd(18)} ${String(c.value).padStart(6)} ${c.unit} (budget <= ${c.budget})`);
  }
}
writeFileSync(path.join(outDir, "results.json"), JSON.stringify({ results, failed }, null, 2));
console.log(`\nResults: ${path.join(outDir, "results.json")}`);
process.exit(failed > 0 ? 1 : 0);
