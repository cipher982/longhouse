#!/usr/bin/env bun
/** Exercise the production session route with isolated HTTP/SSE facts.
 * Transcript content may come from a private capture; states are controlled.
 * This journey proves runtime invalidation/recovery, not transcript replay.
 * No API request can reach the linked Runtime Host. Keep output private.
 */
import assert from "node:assert/strict";
import { createServer, type ServerResponse } from "node:http";
import { Readable } from "node:stream";
import { execFileSync } from "node:child_process";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { chromium, type Browser, type Page } from "playwright";
import { buildSessionDetailStressFixture } from "../ui-fixtures/sessionDetailStress";
import type {
  AgentSession,
  AgentSessionWorkspaceResponse,
} from "../../web/src/services/api/agents";

const args = process.argv.slice(2);
function option(name: string, fallback = "") {
  const index = args.indexOf(name);
  return index < 0 ? fallback : args[index + 1] || fallback;
}
const sourceSha =
  option("--sha") ||
  execFileSync("git", ["rev-parse", "HEAD"], { encoding: "utf8" }).trim();
assert.match(
  sourceSha,
  /^[0-9a-f]{7,40}$/i,
  "source SHA must be a git revision",
);
const vite = new URL(option("--url", "http://127.0.0.1:47213"));
assert.ok(
  ["127.0.0.1", "localhost"].includes(vite.hostname),
  "Use an owned local Vite server",
);
const output = resolve(option("--output", `artifacts/ledger/${Date.now()}`));
const capturePath = option("--capture");
const source = capturePath
  ? JSON.parse(await readFile(resolve(capturePath), "utf8")).workspace
  : buildSessionDetailStressFixture().workspace;
await mkdir(output, { recursive: true });
const errors: string[] = [];
const evidence: Array<Record<string, unknown>> = [];
const apiWrites: string[] = [];
const unknownRoutes = new Set<string>();
let workspace: AgentSessionWorkspaceResponse;
let session: AgentSession;
let sequence = 1;
let refuseStream = false;
let silence = false;
const streams = new Set<ServerResponse>();
const iso = (offset = 0) => new Date(Date.now() + offset).toISOString();

function resetFixture() {
  workspace = structuredClone(source);
  session = workspace.session;
  const available = { state: "available" as const };
  const unavailable = {
    state: "unavailable" as const,
    reason: "not_applicable",
  };
  session.session_state = {
    ...session.session_state,
    state_contract_version: 2,
    presentation_policy_version: 2,
    mode: "helm",
    working_set: "open",
    disposition: { state: "open", closed_at: null, close_reason: null },
    run: {
      id: "ledger-qa-run",
      lifecycle: "running",
      started_at: iso(-60_000),
      ended_at: null,
    },
    activity: {
      state: "executing",
      raw_kind: "running",
      tool: "Read",
      source: "qa",
      observed_at: iso(),
      valid_until: iso(120_000),
    },
    host: { state: "online", observed_at: iso() },
    control: {
      ownership: "owned",
      connection: "connected",
      observed_at: iso(),
      valid_until: iso(120_000),
      actions: {
        start_turn: unavailable,
        send_input: available,
        interrupt: available,
        terminate: available,
        reattach: unavailable,
        resume: unavailable,
        branch: unavailable,
      },
    },
    pending_interaction: null,
    transcript: {
      convergence: "current",
      searchable: true,
      live_observation: true,
      last_append_at: iso(-1000),
    },
    presentation: {
      primary: {
        key: "executing",
        label: "Using Read",
        tone: "running",
        observed_at: iso(),
      },
      access: {
        key: "live_control",
        label: "Live control",
        tone: "live",
        observed_at: iso(),
      },
      transcript: null,
    },
    last_result_at: null,
    last_result_outcome: null,
  };
  session.capabilities = {
    ...session.capabilities,
    live_control_available: true,
    reply_to_live_session_available: true,
    can_queue_next_input: true,
    can_steer_active_turn: true,
  };
  session.is_writable_head = true;
  session.ended_at = null;
  session.transcript_preview = null;
  session.runtime_display = { ...session.runtime_display, pause_request: null };
  workspace.thread.sessions = workspace.thread.sessions.map((item) =>
    item.id === session.id ? session : item,
  );
  refuseStream = false;
}
function emit(event: string, data: unknown, id?: number) {
  for (const response of streams)
    response.write(
      `${id ? `id: ${id}\n` : ""}event: ${event}\ndata: ${JSON.stringify(data)}\n\n`,
    );
}
function publish() {
  session.session_state.commit_seq = ++sequence;
  emit(
    "workspace_changed",
    {
      session_id: session.id,
      pubsub_seq: sequence,
      catalog_commit_seq: sequence,
      change_kind: "runtime",
      latest_event_id: 0,
      server_now_ms: Date.now(),
    },
    sequence,
  );
}
function restoreWork() {
  session.session_state.activity = {
    state: "executing",
    raw_kind: "running",
    tool: "Read",
    source: "qa",
    observed_at: iso(),
    valid_until: iso(120_000),
  };
  session.session_state.presentation.primary = {
    key: "executing",
    label: "Using Read",
    tone: "running",
    observed_at: iso(),
  };
  session.session_state.pending_interaction = null;
  session.runtime_display.pause_request = null;
  publish();
}
resetFixture();
const server = createServer(async (request, response) => {
  try {
    const url = new URL(request.url!, "http://localhost");
    const path = url.pathname;
    const base = `/api/timeline/sessions/${session.id}`;
    const json = (body: unknown, status = 200) => {
      response.writeHead(status, { "Content-Type": "application/json" });
      response.end(JSON.stringify(body));
    };
    if (path === `${base}/workspace/stream`) {
      if (refuseStream) {
        json({ detail: "Controlled connection loss" }, 503);
        return;
      }
      response.writeHead(200, {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        Connection: "keep-alive",
      });
      response.write(
        `event: connected\ndata: ${JSON.stringify({ session_id: session.id, server_now_ms: Date.now() })}\n\n`,
      );
      streams.add(response);
      response.on("close", () => streams.delete(response));
      return;
    }
    if (path.startsWith("/api/")) {
      if (request.method !== "GET") {
        apiWrites.push(`${request.method} ${path}`);
        if (path.endsWith("/read") || path.endsWith("/client-presence")) {
          json({ ok: true });
          return;
        }
        json(
          { detail: "Commands are disabled in the isolated Ledger fixture" },
          403,
        );
        return;
      }
      if (path === base) {
        json(session);
        return;
      }
      if (path === `${base}/workspace`) {
        json(workspace);
        return;
      }
      if (path === `${base}/thread`) {
        json(workspace.thread);
        return;
      }
      if (path === `${base}/projection`) {
        json(workspace.projection);
        return;
      }
      if (path === `${base}/turns`) {
        json({ turns: [], total: 0 });
        return;
      }
      if (path === `${base}/workflows`) {
        json({ session_id: session.id, workflow_runs: [] });
        return;
      }
      if (path === `/api/sessions/${session.id}/lock`) {
        json({ locked: false });
        return;
      }
      if (path === `/api/sessions/${session.id}/inputs`) {
        json([]);
        return;
      }
      if (path === "/api/system/capabilities") {
        json({
          auth_disabled: true,
          llm_available: false,
          embeddings_available: false,
        });
        return;
      }
      if (path === "/api/auth/me") {
        json({ id: 1, email: "qa@example.invalid", display_name: "Local QA" });
        return;
      }
      unknownRoutes.add(path);
      json({ detail: "No fixture for this API" }, 404);
      return;
    }
    if (path === "/config.js") {
      response.writeHead(200, { "Content-Type": "text/javascript" });
      response.end(
        'window.__APP_MODE__="dev";window.API_BASE_URL="/api";window.__SINGLE_TENANT__=true;',
      );
      return;
    }
    const upstream = await fetch(new URL(request.url!, vite));
    response.writeHead(upstream.status, Object.fromEntries(upstream.headers));
    if (upstream.body) Readable.fromWeb(upstream.body as never).pipe(response);
    else response.end();
  } catch (error) {
    response.writeHead(500);
    response.end(String(error));
  }
});
await new Promise<void>((done) => server.listen(0, "127.0.0.1", done));
const address = server.address();
assert.ok(address && typeof address !== "string");
const origin = `http://127.0.0.1:${address.port}`;
console.log(`Ledger QA fixture listening at ${origin}`);
const heartbeat = setInterval(() => {
  if (!silence) emit("heartbeat", { timestamp: iso() });
}, 1000);
let browser: Browser | undefined;
async function motion(page: Page, active: boolean) {
  await page.waitForFunction(
    (expected) => {
      const ribbon = document.querySelector<HTMLElement>(
        '[data-testid="live-work-ribbon"]',
      );
      if (!ribbon) return false;
      return expected
        ? ribbon.dataset.workState === "working" &&
            (matchMedia("(prefers-reduced-motion: reduce)").matches ||
              ribbon.dataset.workMotion === "active")
        : ribbon.dataset.workState !== "working" &&
            ribbon.dataset.workMotion === "off";
    },
    active,
    { timeout: 55_000 },
  );
}
async function shot(page: Page, name: string) {
  await page.waitForTimeout(400);
  const ribbon = page.getByTestId("live-work-ribbon");
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth > innerWidth + 1,
  );
  assert.equal(overflow, false, `${name}: horizontal overflow`);
  await page.screenshot({ path: `${output}/${name}.png` });
  evidence.push({
    name,
    text: await ribbon.innerText(),
    workMotion: await ribbon.getAttribute("data-work-motion"),
    horizontalOverflow: overflow,
  });
}
try {
  browser = await chromium.launch({ headless: true });
  for (const size of args.includes("--inspect")
    ? []
    : [
        {
          name: "desktop",
          width: 1440,
          height: 1000,
          reducedMotion: "no-preference" as const,
          colorScheme: "dark" as const,
        },
        {
          name: "phone",
          width: 390,
          height: 844,
          reducedMotion: "no-preference" as const,
          colorScheme: "dark" as const,
        },
        {
          name: "narrow",
          width: 320,
          height: 740,
          reducedMotion: "reduce" as const,
          colorScheme: "light" as const,
        },
      ]) {
    resetFixture();
    const context = await browser.newContext({
      viewport: size,
      colorScheme: size.colorScheme,
      reducedMotion: size.reducedMotion,
      recordVideo: {
        dir: `${output}/${size.name}-video`,
        size: { width: size.width, height: size.height },
      },
    });
    try {
      await context.route("**/*", (route) => {
        const url = new URL(route.request().url());
        return [origin, vite.origin].includes(url.origin)
          ? route.continue()
          : route.abort();
      });
      const page = await context.newPage();
      page.on("pageerror", (error) =>
        errors.push(`${size.name}: ${error.message}`),
      );
      await page.goto(`${origin}/timeline/${session.id}`);
      await page.getByTestId("live-work-ribbon").waitFor();
      await motion(page, true);
      await page.waitForTimeout(4_200);
      await shot(page, `${size.name}-working`);
      assert.equal(
        await page
          .getByText("Fresh provider evidence restored.", { exact: true })
          .count(),
        0,
        "Initial connection must not announce provider recovery",
      );
      if (size.reducedMotion === "reduce") {
        const reducedMotionFrame = await page.evaluate(() => {
          const ribbon = document.querySelector<HTMLElement>(
            '[data-testid="live-work-ribbon"]',
          );
          const glyph = ribbon?.querySelector<HTMLElement>(
            ".session-ledger__glyph",
          );
          const canvas = ribbon?.querySelector<HTMLCanvasElement>(
            '[data-testid="session-activity-strip"]',
          );
          const context = canvas?.getContext("2d");
          const pixels =
            context && canvas
              ? context.getImageData(0, 0, canvas.width, canvas.height).data
              : null;
          return {
            animationName: glyph ? getComputedStyle(glyph).animationName : "",
            canvasWidth: canvas?.width ?? 0,
            canvasPainted: Boolean(
              pixels &&
              Array.from(pixels).some(
                (value, index) => index % 4 === 3 && value > 0,
              ),
            ),
          };
        });
        assert.equal(
          reducedMotionFrame.animationName,
          "none",
          "Reduced motion freezes the working glyph",
        );
        assert.ok(
          reducedMotionFrame.canvasWidth > 0 &&
            reducedMotionFrame.canvasPainted,
          "Reduced motion still paints the active update canvas",
        );
      }
      assert.ok(
        (await page.getByTestId("session-control-dock").boundingBox())!
          .height <= 120,
        "Ordinary work keeps the combined dock within 120px",
      );
      const draft = page.locator(".session-chat textarea");
      await draft.fill("Keep my draft through evidence changes.");
      await draft.focus();
      const initial = await draft.boundingBox();
      assert.ok(initial);
      const preserveDraft = async () => {
        assert.equal(
          await draft.inputValue(),
          "Keep my draft through evidence changes.",
        );
        assert.equal(
          await draft.evaluate((node) => node === document.activeElement),
          true,
          "Status changes must preserve focus",
        );
        const next = await draft.boundingBox();
        assert.ok(
          next &&
            Math.abs(next.y - initial.y) < 1 &&
            Math.abs(next.height - initial.height) < 1,
          `Status grows without moving the active draft: ${JSON.stringify({ initial, next })}`,
        );
      };
      session.session_state.activity.valid_until = iso(1000);
      publish();
      await motion(page, false);
      await preserveDraft();
      await shot(page, `${size.name}-expired-connected`);
      for (let index = 0; index < 30; index++) publish();
      await page.waitForTimeout(500);
      await motion(page, false);
      await shot(page, `${size.name}-runtime-invalidation-burst`);
      restoreWork();
      await motion(page, true);
      refuseStream = true;
      for (const stream of streams) stream.end();
      await motion(page, false);
      await preserveDraft();
      await shot(page, `${size.name}-disconnected`);
      session.session_state.activity.valid_until = iso(-1000);
      refuseStream = false;
      await page.waitForFunction(
        () =>
          document
            .querySelector('[data-testid="live-work-ribbon"]')
            ?.getAttribute("data-connection") === "connected",
      );
      publish();
      await motion(page, false);
      await shot(page, `${size.name}-reconnected-stale`);
      assert.equal(
        await page
          .getByText("Fresh provider evidence restored.", { exact: true })
          .count(),
        0,
        "Reconnect with stale provider evidence must not announce recovery",
      );
      restoreWork();
      await motion(page, true);
      await preserveDraft();
      await shot(page, `${size.name}-recovered`);
      assert.ok(
        (await page
          .getByText("Fresh provider evidence restored.", { exact: true })
          .count()) > 0,
        "A genuinely refreshed provider observation announces recovery",
      );
      session.session_state.host.state = "offline";
      publish();
      await motion(page, false);
      await shot(page, `${size.name}-host-offline`);
      session.session_state.host.state = "online";
      session.session_state.transcript.convergence = "lagging";
      publish();
      await motion(page, false);
      await shot(page, `${size.name}-transcript-lagging`);
      session.session_state.transcript.convergence = "current";
      restoreWork();
      await motion(page, true);
      if (size.name === "desktop") {
        silence = true;
        await motion(page, false);
        assert.equal(
          await page
            .getByTestId("live-work-ribbon")
            .getAttribute("data-connection"),
          "reconnecting",
        );
        assert.equal(
          await page
            .getByText("Fresh provider evidence restored.", { exact: true })
            .count(),
          0,
          "Heartbeat silence must not announce refreshed provider evidence",
        );
        await shot(page, "desktop-silent-open-socket");
        silence = false;
        await motion(page, true);
      }
      session.session_state.pending_interaction = {
        id: "ledger-approval",
        kind: "approval",
        opened_at: iso(),
        can_respond: false,
      };
      session.runtime_display.pause_request = {
        id: "ledger-approval",
        session_id: session.id,
        runtime_key: "qa",
        provider: session.provider,
        kind: "permission_prompt",
        status: "pending",
        can_respond: false,
        title: "Approve repository changes",
        summary: "Approval must be answered in the provider terminal.",
        tool_name: "shell",
        occurred_at: iso(),
      };
      publish();
      await motion(page, false);
      await preserveDraft();
      await shot(page, `${size.name}-approval-draft`);
      assert.equal(
        await page.getByRole("button", { name: /allow once/i }).count(),
        0,
        "Unsupported remote approval must not be invented",
      );
      restoreWork();
      await motion(page, true);
      const disclosure = page
        .getByTestId("live-work-ribbon")
        .locator("summary");
      await disclosure.click();
      publish();
      await page.waitForTimeout(500);
      assert.ok(
        await page
          .getByTestId("live-work-ribbon")
          .locator("details[open]")
          .count(),
        "A receipt must not close manual evidence",
      );
      await shot(page, `${size.name}-evidence`);
      await disclosure.click();
      session.session_state.activity.state = "quiescent";
      session.session_state.presentation.primary = {
        key: "finished",
        label: "Turn ended",
        tone: "idle",
        observed_at: iso(),
      };
      session.session_state.last_result_at = iso();
      session.session_state.last_result_outcome = "success";
      publish();
      await motion(page, false);
      await shot(page, `${size.name}-finished-notice`);
      await page.waitForTimeout(4_200);
      assert.equal(
        await page
          .getByTestId("live-work-ribbon")
          .getAttribute("data-prominence"),
        "rest",
      );
      await shot(page, `${size.name}-finished-settled`);
      restoreWork();
      await motion(page, true);
      const scroll = page.locator(".timeline-events");
      await scroll.evaluate((node) => {
        node.scrollTop = Math.max(0, node.scrollHeight / 3);
      });
      const scrollBefore = await scroll.evaluate((node) => node.scrollTop);
      session.session_state.activity.valid_until = iso(-1);
      publish();
      await motion(page, false);
      await page.waitForTimeout(400);
      assert.ok(
        Math.abs(
          (await scroll.evaluate((node) => node.scrollTop)) - scrollBefore,
        ) < 2,
        "Status updates preserve scrolled-away reading",
      );
      if (size.reducedMotion === "reduce") {
        const animated = await page.evaluate(
          () =>
            document
              .getAnimations()
              .filter(
                (a) =>
                  a.playState === "running" &&
                  a.effect instanceof KeyframeEffect &&
                  a.effect.target instanceof Element &&
                  a.effect.target.closest('[data-testid="live-work-ribbon"]'),
              ).length,
        );
        assert.equal(animated, 0, "Reduced motion stops status animation");
      }
      evidence.push({
        name: `${size.name}-interactions`,
        draftRetained: true,
        focusRetained: true,
        scrollRetained: true,
      });
    } catch (error) {
      const failedPage = context.pages()[0];
      if (failedPage) {
        await failedPage.screenshot({
          path: `${output}/${size.name}-failure.png`,
        });
        await writeFile(
          `${output}/${size.name}-failure.txt`,
          await failedPage.locator("body").innerText(),
        );
      }
      throw error;
    } finally {
      await context.close();
    }
  }
  if (args.includes("--inspect")) {
    console.log(`Inspect session: ${origin}/timeline/${session.id}`);
    await new Promise<void>((done) => process.once("SIGTERM", done));
  }
  assert.deepEqual(errors, [], "Production route must not raise page errors");
} catch (error) {
  errors.push(String(error));
  throw error;
} finally {
  clearInterval(heartbeat);
  for (const stream of streams) stream.end();
  await browser?.close();
  server.closeAllConnections();
  await new Promise<void>((done) => server.close(() => done()));
  await writeFile(
    `${output}/manifest.json`,
    JSON.stringify(
      {
        source_sha: sourceSha,
        source: capturePath
          ? "private recorded transcript metadata; runtime invalidation journeys (not transcript replay)"
          : "synthetic fixture; runtime invalidation journeys (not transcript replay)",
        errors,
        evidence,
        apiWrites,
        unknownRoutes: [...unknownRoutes],
      },
      null,
      2,
    ),
  );
}
console.log(
  JSON.stringify(
    { output, source_sha: sourceSha, checks: evidence.length, errors },
    null,
    2,
  ),
);
