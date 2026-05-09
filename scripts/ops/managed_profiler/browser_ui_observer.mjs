import { chromium } from "playwright";

const [baseUrlArg, token, sid, project, nonce] = process.argv.slice(2);

if (!baseUrlArg || !token || !sid || !project || !nonce) {
  console.error(
    "usage: browser_ui_observer.mjs <base-url> <session-cookie> <session-id> <project> <nonce>",
  );
  process.exit(2);
}

const baseUrl = new URL(baseUrlArg);
const started = performance.now();
const onceKinds = new Set([
  "ui_loaded",
  "card_painted",
  "live_first_painted",
  "live_word_painted",
  "live_nonce_painted",
  "close_painted",
]);
const emitted = new Set();
let browser;
let page;
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

async function afterPaint() {
  if (!page) {
    return;
  }
  await page.evaluate(
    () =>
      new Promise((resolve) => {
        requestAnimationFrame(() => requestAnimationFrame(resolve));
      }),
  );
}

async function waitForCard(kind, timeoutMs) {
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

        const live = card.querySelector('[data-testid="session-card-live-transcript"]');
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
          live_text: live?.textContent?.trim() ?? "",
          closed_text: closed?.textContent?.trim() ?? "",
          runtime_text: runtime?.textContent?.trim() ?? "",
        };

        if (targetKind === "card_painted") {
          return snapshot;
        }
        if (targetKind === "live_first_painted" && snapshot.live_text) {
          return snapshot;
        }
        if (targetKind === "live_word_painted" && /\b\S+\b/.test(snapshot.live_text)) {
          return snapshot;
        }
        if (targetKind === "live_nonce_painted" && snapshot.live_text.includes(targetNonce)) {
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
      { sessionId: sid, targetKind: kind, targetNonce: nonce },
      { timeout: timeoutMs, polling: "raf" },
    );
    const domMatchedElapsedMs = elapsedMs();
    const card = await handle.jsonValue();
    await handle.dispose();
    if (kind === "close_painted") {
      closeObserved = true;
    }
    await afterPaint();
    emit(kind, { dom_matched_elapsed_ms: domMatchedElapsedMs, card });
  } catch (error) {
    if (!closeObserved) {
      emit(`${kind}_timeout`, { error: String(error).slice(0, 500) });
    }
  }
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
  await page.goto(url.toString(), { waitUntil: "domcontentloaded", timeout: 30000 });
  await afterPaint();
  emit("ui_loaded", { url: page.url() });

  void waitForCard("card_painted", 30000);
  void waitForCard("live_first_painted", 95000);
  void waitForCard("live_word_painted", 95000);
  void waitForCard("live_nonce_painted", 95000);
  await waitForCard("close_painted", 420000);
} catch (error) {
  emit("error", { error: String(error).slice(0, 1000) });
} finally {
  if (browser) {
    await browser.close();
  }
}
