// Record what a user sees, step by step, when they send into a live session.
//
// Drives the real web UI against a disposable local Runtime Host (never a
// hosted tenant) and writes one merged timeline: rendered bubble state,
// activity headline, composer badge, transcript user rows, the input POSTs,
// and the server's own receipt/turn state, all on one clock.
//
//   node e2e/tools/send-timeline.mjs --web http://127.0.0.1:47201 \
//     --api http://127.0.0.1:47311 --session <id> --scenario single|midturn
import { chromium } from "@playwright/test";

const args = Object.fromEntries(
  process.argv.slice(2).reduce((pairs, arg, i, all) => {
    if (arg.startsWith("--")) pairs.push([arg.slice(2), all[i + 1]]);
    return pairs;
  }, []),
);
const WEB = args.web;
const API = args.api;
const SESSION = args.session;
const SCENARIO = args.scenario ?? "single";
const OUT = args.out ?? `/tmp/agents/send-timeline/timeline-${SCENARIO}-${Date.now()}.jsonl`;
for (const [name, value] of Object.entries({ WEB, API, SESSION })) {
  if (!value) throw new Error(`missing --${name.toLowerCase()}`);
}
for (const url of [WEB, API]) {
  const host = new URL(url).hostname;
  if (!["127.0.0.1", "localhost", "::1"].includes(host)) {
    throw new Error(`refusing non-loopback target ${url}`);
  }
}

const t0 = Date.now();
let scenarioFailed = false;
const events = [];
const fs = await import("node:fs");
function record(source, what, detail = {}) {
  const row = { t: Date.now() - t0, source, what, ...detail };
  events.push(row);
  fs.appendFileSync(OUT, JSON.stringify(row) + "\n");
  const tail = Object.keys(detail).length ? " " + JSON.stringify(detail) : "";
  console.log(`${String(row.t).padStart(6)}ms ${source.padEnd(6)} ${what}${tail}`);
}

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });

page.on("request", (req) => {
  const url = req.url();
  if (/\/api\/sessions\/[^/]+\/(input|inputs-multipart|send-live)$/.test(url)) {
    record("net", "POST start", { path: new URL(url).pathname.split("/").pop(), body: req.postData()?.slice(0, 160) });
  }
});
page.on("response", async (res) => {
  const url = res.url();
  if (/\/api\/sessions\/[^/]+\/(input|inputs-multipart|send-live)$/.test(url)) {
    let body = "";
    try {
      body = (await res.text()).slice(0, 400);
    } catch {}
    record("net", "POST end", { status: res.status(), body });
  }
});
page.on("requestfailed", (req) => {
  if (/\/api\/sessions\//.test(req.url())) record("net", "request failed", { url: new URL(req.url()).pathname, error: req.failure()?.errorText });
});

// Rendered state, sampled every 50 ms; only changes are recorded.
async function snapshot() {
  return page.evaluate(() => {
    const text = (el) => (el ? el.textContent.replace(/\s+/g, " ").trim() : null);
    const outbox = [...document.querySelectorAll("[data-testid=session-outbox-row]")].map(
      (el) => `${el.getAttribute("data-outbox-state")}:${text(el.querySelector(".tl-msg__outbox-status"))}`,
    );
    const userRows = [...document.querySelectorAll(".tl-msg--user:not(.tl-msg--outbox)")].map((el) =>
      text(el).slice(0, 40),
    );
    const assistantEls = [...document.querySelectorAll(".tl-msg--assistant, [data-role=assistant]")];
    const assistantRows = assistantEls.length;
    const lastAssistant = assistantEls.length ? text(assistantEls.at(-1)).slice(-60) : null;
    const head = text(document.querySelector("[data-testid=session-chat-composer-head] .session-chat-composer__head-label"));
    const turnEnded = !!document.querySelector("[data-testid=session-chat-turn-ended]");
    const sendButton = [...document.querySelectorAll(".session-chat-composer button")]
      .map((b) => text(b))
      .filter((t) => t && /send|queue|update|stop/i.test(t))
      .join("|");
    const notice = text(document.querySelector(".session-chat-sent-notice"));
    return { outbox: outbox.join(" ; "), assistantRows, lastAssistant, userRows: userRows.length, lastUser: userRows.at(-1) ?? null, head, turnEnded, sendButton, notice };
  });
}

let last = {};
let sampling = true;
async function sampleLoop() {
  while (sampling) {
    try {
      const snap = await snapshot();
      for (const [key, value] of Object.entries(snap)) {
        if (JSON.stringify(value) !== JSON.stringify(last[key])) record("ui", key, { value });
      }
      last = snap;
    } catch {}
    await new Promise((r) => setTimeout(r, 50));
  }
}

// Server truth: receipts and Console turn state, polled every 150 ms.
let lastServer = "";
async function serverLoop() {
  while (sampling) {
    try {
      const res = await fetch(`${API}/api/sessions/${SESSION}/inputs`);
      const rows = await res.json();
      const brief = (Array.isArray(rows) ? rows : rows.inputs ?? [])
        .map((r) => `${(r.text ?? "").slice(0, 18)}|${r.status}|turn=${r.turn?.state ?? "-"}`)
        .join(" ; ");
      if (brief !== lastServer) record("server", "inputs", { value: brief });
      lastServer = brief;
    } catch (error) {
      record("server", "inputs error", { error: String(error) });
    }
    await new Promise((r) => setTimeout(r, 150));
  }
}

async function send(text, label = /^(Send|Queue next|Send now)$/) {
  const box = page.locator(".session-chat-composer textarea");
  await box.fill(text);
  record("user", "click send", { text: text.slice(0, 60), button: String(label) });
  const button = page.locator(".session-chat-composer button", { hasText: label }).first();
  await button.click();
}

async function waitFor(predicate, timeoutMs, label) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    if (predicate()) return true;
    await new Promise((r) => setTimeout(r, 100));
  }
  record("driver", `timeout waiting for ${label}`);
  return false;
}

await page.goto(`${WEB}/timeline/${SESSION}`);
await page.locator(".session-chat-composer textarea").waitFor({ timeout: 30000 });
await new Promise((r) => setTimeout(r, 1500));
const loops = [sampleLoop(), serverLoop()];
await new Promise((r) => setTimeout(r, 300));
const assistantBefore = last.assistantRows ?? 0;

if (SCENARIO === "single") {
  await send("Run `ls -la` in the working directory using your shell tool, then reply with one short sentence naming the files.");
  await waitFor(() => last.turnEnded || /idle/i.test(last.head ?? ""), 180000, "turn end");
} else if (SCENARIO === "steer") {
  // Enter the running turn mid-tool: the same turn must answer the steer.
  await send("Run the shell command `sleep 20 && echo first-done`, then reply with exactly: FIRST DONE");
  await waitFor(() => /using|running|shell|exec/i.test(last.head ?? ""), 60000, "agent running a tool");
  await new Promise((r) => setTimeout(r, 1500));
  await send("Change of plan: when the command finishes, reply with exactly STEERED OK instead.", /^Send update$/);
  await waitFor(() => /idle/i.test(last.head ?? "") && !last.outbox, 180000, "settled after steer");
  const steered = /STEERED OK/.test(last.lastAssistant ?? "");
  record("driver", steered ? "steer answered in-turn" : "steer NOT reflected in the answer", { lastAssistant: last.lastAssistant });
  if (!steered) scenarioFailed = true;
} else if (SCENARIO === "restart") {
  // A deploy: the Runtime Host goes away just before the send and comes back
  // a few seconds later. --down / --up are shell commands that do that.
  const { execSync } = await import("node:child_process");
  record("driver", "server down");
  execSync(args.down, { stdio: "inherit" });
  await send("Sent during a restart: reply with exactly RESTART OK");
  await new Promise((r) => setTimeout(r, 5000));
  record("driver", "server up");
  execSync(args.up, { stdio: "inherit" });
  await waitFor(() => (last.assistantRows ?? 0) > assistantBefore && /idle/i.test(last.head ?? "") && !last.outbox, 120000, "settled after restart");
} else if (SCENARIO === "midtool") {
  await send("Run the shell command `sleep 15 && echo first-done`, then reply with exactly: FIRST DONE");
  await waitFor(() => /using|running|shell|exec/i.test(last.head ?? ""), 60000, "agent running a tool");
  await new Promise((r) => setTimeout(r, 1500));
  await send("Second message sent while a tool runs: reply with exactly SECOND DONE");
  await waitFor(() => /idle/i.test(last.head ?? "") && !last.outbox, 120000, "settled");
} else if (SCENARIO === "midturn") {
  await send("Run the shell command `sleep 12 && echo first-done`, then reply with exactly: FIRST DONE");
  await waitFor(() => /work|think|run|execut/i.test(last.head ?? ""), 60000, "agent working");
  await new Promise((r) => setTimeout(r, 3000));
  await send("Second message sent mid-turn: after the first task, reply with exactly: SECOND DONE");
  await waitFor(() => (last.lastUser ?? "").includes("Second message") && /idle/i.test(last.head ?? ""), 240000, "second turn end");
}
await new Promise((r) => setTimeout(r, 6000));
sampling = false;
await Promise.all(loops);
await page.screenshot({ path: OUT.replace(/\.jsonl$/, ".png"), fullPage: false });
await browser.close();

// The ordering rule: once the server accepts a send, the user's row must stop
// saying "Sending…" before (or as) the agent shows any activity, and no send
// may end up "Not delivered".
const firstAfter = (t, pred) => events.find((e) => e.t >= t && pred(e));
const verdicts = [];
const clicks = events.filter((e) => e.source === "user");
for (const [index, click] of clicks.entries()) {
  const until = clicks[index + 1]?.t ?? Infinity;
  // Judge the send by its last attempt: automatic same-ID retries may follow
  // a transient 502 before the server accepts it.
  const attempts = events.filter((e) => e.t >= click.t && e.t < until && e.what === "POST end");
  const accepted = attempts.at(-1);
  const busy = firstAfter(click.t, (e) => e.what === "head" && e.value && !/^idle$/i.test(e.value));
  const stillSending = events.filter(
    (e) => accepted && e.t > accepted.t + 250 && e.t < until && e.what === "outbox" && /sending:/.test(e.value ?? ""),
  );
  const failed = events.find((e) => e.t >= click.t && e.t < until && e.what === "outbox" && /failed:/.test(e.value ?? ""));
  // The ordering rule: a send that started a turn reads "Sent" (or its echo
  // already replaced it) no later than the agent first shows activity.
  let sentBeforeBusy = true;
  const headAtClick = [...events].reverse().find((e) => e.t < click.t && e.what === "head");
  const idleAtClick = !headAtClick || /^idle$/i.test(headAtClick.value ?? "");
  if (idleAtClick && accepted?.status === 200 && /"outcome":"sent"/.test(accepted.body ?? "")) {
    // Anchor on the click: the DOM can flip before Playwright reports the
    // response, and "Sending…" can be shorter than one sample.
    const settled = firstAfter(click.t, (e) => e.what === "outbox" && /(^|; )(sent|queued):/.test(e.value ?? ""));
    const busy = firstAfter(click.t, (e) => e.what === "head" && e.value && !/^idle$/i.test(e.value));
    sentBeforeBusy = Boolean(settled) && (!busy || settled.t <= busy.t);
  }
  verdicts.push({
    text: click.text,
    post_ms: accepted ? accepted.t - click.t : null,
    attempts: attempts.length,
    status: accepted?.status ?? null,
    agent_busy_ms: busy ? busy.t - click.t : null,
    sending_after_accept: stillSending.length,
    failed: Boolean(failed),
    sent_before_busy: sentBeforeBusy,
  });
}
const ok = verdicts.every((v) => v.status === 200 && v.sending_after_accept === 0 && !v.failed && v.sent_before_busy);
record("driver", ok ? "PASS" : "FAIL", { verdicts });
record("driver", "done", { out: OUT });
process.exitCode = ok && !scenarioFailed ? 0 : 1;
