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
 *                     e.g. "You" to "You asked at 08:55 PM"         (budget 0)
 *   max jump          largest frame-to-frame move of the row within
 *                     the transcript (scrolling is not a move)      (budget 2 px)
 *
 * Output: results.json, frames-<scenario>.json, and a PNG strip of each
 * distinct frame state under --out (default /tmp/agents/web-send-handoff/<stamp>).
 */
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

  // Every element that shows the token, every frame. A WeakMap gives each DOM
  // node a stable id, so a remount (new key, new component) is a new id even
  // when it looks the same.
  const ids = new WeakMap();
  let nextId = 1;
  let token = null;
  let frames = [];
  let sampling = false;
  let t0 = 0;
  const label = (el) => (el ? el.textContent.replace(/\\s+/g, " ").trim() : "");
  // The header without its delivery status and actions: the status word is
  // meant to change ("Sending…", "Sent", then gone); the rest is the row's shape.
  const shape = (el) => {
    if (!el) return "";
    const copy = el.cloneNode(true);
    copy.querySelectorAll("[role=status], button").forEach((node) => node.remove());
    return label(copy);
  };
  function read() {
    const scroller = document.querySelector(".timeline-pane__list");
    const scrollerTop = scroller ? scroller.getBoundingClientRect().top : 0;
    const rows = Array.from(document.querySelectorAll(".timeline-pane__rows > *"));
    const shown = [];
    rows.forEach((el, index) => {
      if (!token || !(el.textContent || "").includes(token)) return;
      if (!ids.has(el)) ids.set(el, nextId++);
      const r = el.getBoundingClientRect();
      shown.push({
        node: ids.get(el),
        kind: el.matches("[data-testid=session-outbox-row]") ? "outbox:" + el.getAttribute("data-outbox-state") : "transcript",
        head: shape(el.querySelector(".tl-msg__head")),
        status: label(el.querySelector(".tl-msg__head [role=status]")),
        fromEnd: rows.length - 1 - index,
        top: Math.round(r.top),
        // Position within the transcript: moves only when rows above it change.
        docTop: Math.round(r.top - scrollerTop + (scroller ? scroller.scrollTop : 0)),
        height: Math.round(r.height),
      });
    });
    return {
      t: Math.round(performance.now() - t0),
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
    start(t) { token = t; frames = []; t0 = performance.now(); sampling = true; requestAnimationFrame(tick); },
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
}

class Harness {
  context!: BrowserContext;
  page!: Page;
  fixture = buildSessionProseIdleFixture();
  workspace = structuredClone(this.fixture.workspace) as { projection: { items: unknown[]; total?: number } } & Record<string, unknown>;
  sessionId = this.fixture.session.id as string;
  state: ServerState = { receipt: null, projectionHasUserRow: false };
  clientRequestId: string | null = null;
  postedAt = 0;
  nextEventId = 990_000;
  log: Array<{ t: number; what: string }> = [];

  note(what: string) {
    this.log.push({ t: this.postedAt ? Date.now() - this.postedAt : -1, what });
  }

  json(route: Route, body: unknown) {
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  }

  async open(browser: Browser, scenario: Scenario): Promise<void> {
    this.context = await browser.newContext({ viewport: { width: 1440, height: 900 }, reducedMotion: "reduce" });
    await this.context.addInitScript(INIT_SCRIPT);
    const base = `/api/timeline/sessions/${this.sessionId}`;
    await this.context.route(`${BASE_URL}/api/**`, async (route) => {
      const url = new URL(route.request().url());
      const p = url.pathname;
      const method = route.request().method();
      if (p === base) return this.json(route, this.fixture.session);
      if (p === `${base}/thread`) return this.json(route, this.fixture.thread);
      if (p === `${base}/workspace` || p === `${base}/projection`) {
        // The server reads its state when the request arrives and answers later.
        const body = structuredClone(p.endsWith("/workspace") ? this.workspace : this.workspace.projection);
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
    await this.page.goto(`${BASE_URL}/timeline/${this.sessionId}`, { waitUntil: "domcontentloaded" });
    await this.page.locator('[data-testid="session-timeline-row"]').first().waitFor({ state: "visible", timeout: 15_000 });
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
  async ingestUserRow(withOrigin: boolean) {
    const id = this.nextEventId++;
    const ts = new Date(this.postedAt).toISOString();
    const event = makeEvent(id, "user", ts, {
      content_text: MESSAGE,
      ...(withOrigin
        ? { input_origin: { authored_via: "longhouse", origin: "user", client_request_id: this.clientRequestId, session_input_id: null } }
        : {}),
    } as never);
    this.workspace.projection.items.push(projectionEvent(event, this.sessionId));
    this.workspace.projection.total = this.workspace.projection.items.length;
    this.state.projectionHasUserRow = true;
    await this.emit({ change_kind: "ingest", latest_event_id: id });
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
  out.push({ at: s.ingestAt, what: `user row ingested${s.ingestOrigin ? "" : " (no origin)"}`, apply: (h) => h.ingestUserRow(s.ingestOrigin) });
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
];

// --sweep: every combination of when each fact lands. The rule must hold for
// all of them, not only the orderings someone thought to write down.
function sweep(): Scenario[] {
  const out: Scenario[] = [];
  for (const postStatus of ["delivered", "delivering"] as const)
    for (const linkAt of [300, 1200, undefined])
      for (const thinkingAt of [400, undefined])
        for (const ingestAt of [150, 900, 2600])
          for (const fetchLatencyMs of [50, 800]) {
            const deliverAt = postStatus === "delivering" ? Math.min(linkAt ?? 700, 700) : undefined;
            out.push({
              name: `sweep_${postStatus}_link${linkAt ?? "never"}_think${thinkingAt ?? "none"}_ingest${ingestAt}_read${fetchLatencyMs}`,
              about: "generated ordering",
              postStatus,
              deliverAt,
              linkAt,
              thinkingAt,
              ingestAt,
              ingestOrigin: linkAt != null,
              fetchLatencyMs,
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
  return {
    checks: [
      le("appeared", first < 0 ? 1 : 0, 0, "bool"),
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
