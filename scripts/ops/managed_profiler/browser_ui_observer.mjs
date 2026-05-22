import { chromium } from "playwright";
import { promises as fs } from "fs";

const [baseUrlArg, token, sid, project, nonce] = process.argv.slice(2);

if (!baseUrlArg || !token || !sid || !project || !nonce) {
  console.error(
    "usage: browser_ui_observer.mjs <base-url> <session-cookie> <session-id> <project> <nonce>",
  );
  process.exit(2);
}

const baseUrl = new URL(baseUrlArg);
let sessionId = sid;
const sessionIdFile = process.env.LONGHOUSE_BROWSER_OBSERVER_SESSION_ID_FILE || "";
const started = performance.now();
const onceKinds = new Set([
  "ui_loaded",
  "navigation_started",
  "card_painted",
  "preview_first_painted",
  "preview_word_painted",
  "preview_nonce_painted",
  "close_painted",
  "detail_loaded",
  "detail_navigation_started",
  "timeline_page_closed_after_card",
]);
const emitted = new Set();
const exitAfterDetailTranscript =
  process.env.LONGHOUSE_BROWSER_OBSERVER_EXIT_AFTER_DETAIL_TRANSCRIPT === "1";
let browser;
let page;
let detailPage;
let closeObserved = false;

function elapsedMs() {
  return Math.round(performance.now() - started);
}

function emit(kind, payload = {}) {
  if (onceKinds.has(kind) && emitted.has(kind)) {
    return;
  }
  if (onceKinds.has(kind)) {
    emitted.add(kind);
  }
  console.log(
    JSON.stringify({
      kind,
      elapsed_ms: elapsedMs(),
      observer_observed_at_wall: new Date().toISOString(),
      ...payload,
    }),
  );
}

async function afterPaintOn(targetPage) {
  if (!targetPage) {
    return;
  }
  await targetPage.evaluate(
    () =>
      new Promise((resolve) => {
        requestAnimationFrame(() => requestAnimationFrame(resolve));
      }),
  );
}

async function afterPaint() {
  await afterPaintOn(page);
}

async function waitForCard(kind, timeoutMs) {
  if (!page || page.isClosed()) {
    emit(`${kind}_timeout`, { error: "timeline page is closed" });
    return false;
  }
  try {
    const handle = await page.waitForFunction(
      ({ sessionId, targetKind, targetNonce }) => {
        const escaped = CSS.escape(sessionId);
        const card = document.querySelector(
          `[data-session-id="${escaped}"], [data-thread-id="${escaped}"]`,
        );
        if (!card) {
          return false;
        }

        const preview = card.querySelector('[data-testid="session-card-transcript-preview"]');
        const closed = card.querySelector('[data-testid="session-card-closed-state"]');
        const runtime = card.querySelector('[data-testid="session-card-runtime"]');
        const snapshot = {
          session_id: card.getAttribute("data-session-id"),
          thread_id: card.getAttribute("data-thread-id"),
          card_state: card.getAttribute("data-card-state"),
          runtime_tone: card.getAttribute("data-runtime-tone"),
          runtime_freshness: card.getAttribute("data-runtime-freshness"),
          control_path: card.getAttribute("data-control-path"),
          page_observed_at_wall: new Date().toISOString(),
          page_performance_now_ms: performance.now(),
          preview_text: preview?.textContent?.trim() ?? "",
          closed_text: closed?.textContent?.trim() ?? "",
          runtime_text: runtime?.textContent?.trim() ?? "",
        };

        if (targetKind === "card_painted") {
          return snapshot;
        }
        if (targetKind === "preview_first_painted" && snapshot.preview_text) {
          return snapshot;
        }
        if (targetKind === "preview_word_painted" && /\b\S+\b/.test(snapshot.preview_text)) {
          return snapshot;
        }
        if (targetKind === "preview_nonce_painted" && snapshot.preview_text.includes(targetNonce)) {
          return snapshot;
        }
        if (
          targetKind === "close_painted" &&
          (snapshot.card_state === "closed" || snapshot.closed_text)
        ) {
          return snapshot;
        }
        return false;
      },
      { sessionId, targetKind: kind, targetNonce: nonce },
      { timeout: timeoutMs, polling: "raf" },
    );
    const domMatchedElapsedMs = elapsedMs();
    const card = await handle.jsonValue();
    await handle.dispose();
    if (kind === "close_painted") {
      closeObserved = true;
    }
    await afterPaint();
    const paintStamp = await page.evaluate(() => ({
      page_painted_at_wall: new Date().toISOString(),
      page_painted_performance_now_ms: performance.now(),
    }));
    emit(kind, { dom_matched_elapsed_ms: domMatchedElapsedMs, card: { ...card, ...paintStamp } });
    return true;
  } catch (error) {
    if (!closeObserved) {
      emit(`${kind}_timeout`, { error: String(error).slice(0, 500) });
    }
    return false;
  }
}

async function waitForDetailTranscript(kind, timeoutMs) {
  try {
    const handle = await detailPage.waitForFunction(
      ({ targetKind, targetNonce }) => {
        const rows = Array.from(
          document.querySelectorAll(
            '[data-testid="session-timeline-row"][data-row-kind="message"][data-message-role="assistant"]',
          ),
        );
        const snapshots = rows.map((row) => {
          const body = row.querySelector(".tl-msg__body");
          return {
            row_id: row.getAttribute("id"),
            page_observed_at_wall: new Date().toISOString(),
            page_performance_now_ms: performance.now(),
            text: body?.textContent?.trim() ?? row.textContent?.trim() ?? "",
          };
        });
        const match = snapshots.find((snapshot) =>
          targetKind === "live_transcript_nonce_painted"
            ? snapshot.text.includes(targetNonce)
            : /\b\S+\b/.test(snapshot.text),
        );
        return match || false;
      },
      { targetKind: kind, targetNonce: nonce },
      { timeout: timeoutMs, polling: "raf" },
    );
    const domMatchedElapsedMs = elapsedMs();
    const transcript = await handle.jsonValue();
    await handle.dispose();
    await afterPaintOn(detailPage);
    const paintStamp = await detailPage.evaluate(() => ({
      page_painted_at_wall: new Date().toISOString(),
      page_painted_performance_now_ms: performance.now(),
    }));
    emit(kind, { dom_matched_elapsed_ms: domMatchedElapsedMs, transcript: { ...transcript, ...paintStamp } });
    return true;
  } catch (error) {
    if (!closeObserved) {
      emit(`${kind}_timeout`, { error: String(error).slice(0, 500) });
    }
    return false;
  }
}

async function openDetailObserver(context) {
  detailPage = await context.newPage();
  detailPage.on("console", (message) => {
    const type = message.type();
    if (type === "error" || type === "warning") {
      emit("detail_console", { level: type, text: message.text().slice(0, 500) });
    }
  });
  detailPage.on("pageerror", (error) => {
    emit("detail_page_error", { error: String(error).slice(0, 1000) });
  });

  const url = new URL(`/timeline/${sessionId}`, baseUrl);
  emit("detail_navigation_started", { url: url.toString() });
  await detailPage.goto(url.toString(), { waitUntil: "domcontentloaded", timeout: 30000 });
  await afterPaintOn(detailPage);
  emit("detail_loaded", { url: detailPage.url() });
}

async function waitForSessionIdFile(timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const value = (await fs.readFile(sessionIdFile, "utf8")).trim();
      if (value) {
        return value;
      }
    } catch {
      // Keep waiting for the harness to publish the managed session id.
    }
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  throw new Error(`timed out waiting for session id file: ${sessionIdFile}`);
}

try {
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  await context.addCookies([
    {
      name: "longhouse_session",
      value: token,
      domain: baseUrl.hostname,
      path: "/",
      httpOnly: false,
      secure: baseUrl.protocol === "https:",
      sameSite: "Lax",
    },
  ]);

  page = await context.newPage();
  await page.exposeFunction("__longhouseProfilerTimelineStreamEvent", (detail) => {
    if (!detail || typeof detail !== "object") {
      return;
    }
    const kind = typeof detail.kind === "string" ? detail.kind : "unknown";
    emit(`timeline_stream_${kind}`, { detail });
  });
  await page.addInitScript(() => {
    window.addEventListener("longhouse:timeline-stream", (event) => {
      window.__longhouseProfilerTimelineStreamEvent?.(event.detail);
    });
  });
  page.on("console", (message) => {
    const type = message.type();
    if (type === "error" || type === "warning") {
      emit("console", { level: type, text: message.text().slice(0, 500) });
    }
  });
  page.on("pageerror", (error) => {
    emit("page_error", { error: String(error).slice(0, 1000) });
  });

  const url = new URL("/timeline", baseUrl);
  url.searchParams.set("project", project);
  url.searchParams.set("provider", "codex");
  url.searchParams.set("limit", "20");
  url.searchParams.set("hide_autonomous", "true");
  emit("navigation_started", { url: url.toString() });
  await page.goto(url.toString(), { waitUntil: "domcontentloaded", timeout: 30000 });
  await afterPaint();
  emit("ui_loaded", { url: page.url() });

  if (sessionId === "-") {
    if (!sessionIdFile) {
      throw new Error("sid '-' requires LONGHOUSE_BROWSER_OBSERVER_SESSION_ID_FILE");
    }
    emit("awaiting_session_id", { session_id_file: sessionIdFile });
    sessionId = await waitForSessionIdFile(60000);
    emit("session_id_received", { session_id: sessionId });
  }

  const cardPainted = await waitForCard("card_painted", 30000);
  if (exitAfterDetailTranscript && page && !page.isClosed()) {
    await page.close();
    page = undefined;
    emit("timeline_page_closed_after_card", { card_painted: cardPainted });
  }

  await openDetailObserver(context);
  const detailFirstPainted = waitForDetailTranscript("live_transcript_first_painted", 95000);
  const detailNoncePainted = waitForDetailTranscript("live_transcript_nonce_painted", 95000);
  if (exitAfterDetailTranscript) {
    await Promise.all([detailFirstPainted, detailNoncePainted]);
  } else {
    void detailFirstPainted;
    void detailNoncePainted;
    await waitForCard("close_painted", 420000);
  }
} catch (error) {
  emit("error", { error: String(error).slice(0, 1000) });
} finally {
  if (browser) {
    await browser.close();
  }
}
