import { chromium } from "playwright";
import { promises as fs } from "fs";

const [baseUrlArg, token, sid, project, nonce, providerArg] = process.argv.slice(2);

if (!baseUrlArg || !token || !sid || !project || !nonce) {
  console.error(
    "usage: browser_ui_observer.mjs <base-url> <session-cookie> <session-id> <project> <nonce> [provider]",
  );
  process.exit(2);
}

const baseUrl = new URL(baseUrlArg);
const provider = providerArg || "codex";
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
  "detail_workspace_request",
  "detail_workspace_response",
  "detail_workspace_failed",
  "detail_workspace_root_ready",
  "detail_workspace_stream_ready",
  "timeline_page_closed_after_card",
]);
const emitted = new Set();
const exitAfterDetailTranscript =
  process.env.LONGHOUSE_BROWSER_OBSERVER_EXIT_AFTER_DETAIL_TRANSCRIPT === "1";
let browser;
let page;
let detailPage;
let closeObserved = false;
let observerClosing = false;
const runtimeSettlementTargets = new Set();
let runtimeSettlementChain = Promise.resolve();

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

function clientRenderBeaconPayload(request) {
  const raw = request.postData();
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    const values = Array.isArray(parsed) ? parsed : [parsed];
    return values
      .filter((value) => value && typeof value === "object")
      .map((value) => ({
        event_id: value.event_id ?? null,
        session_id: value.session_id ?? null,
        surface: value.surface ?? null,
        render_kind: value.render_kind ?? null,
        emitted_at_ms: value.emitted_at_ms ?? null,
        rendered_at_ms: value.rendered_at_ms ?? null,
        clock_skew_ms: value.clock_skew_ms ?? null,
        server_fanout_at_ms: value.server_fanout_at_ms ?? null,
        client_received_at_ms: value.client_received_at_ms ?? null,
        pubsub_seq: value.pubsub_seq ?? null,
        state_commit_seq: value.state_commit_seq ?? null,
        state_phase: value.state_phase ?? null,
        state_observed_at_ms: value.state_observed_at_ms ?? null,
      }));
  } catch {
    return [];
  }
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
          card_state:
            card.getAttribute("data-card-state") ||
            (card.getAttribute("data-closed") === "true" ? "closed" : null),
          runtime_tone: card.getAttribute("data-runtime-tone") || card.getAttribute("data-status"),
          runtime_freshness: card.getAttribute("data-runtime-freshness"),
          control_path: card.getAttribute("data-control-path"),
          activity_state: card.getAttribute("data-activity-state"),
          activity_observed_at: card.getAttribute("data-activity-observed-at"),
          state_commit_seq: card.getAttribute("data-state-commit-seq"),
          page_observed_at_wall: new Date().toISOString(),
          page_performance_now_ms: performance.now(),
          preview_text:
            preview?.textContent?.trim() ??
            card.querySelector('[data-testid="session-row-snippet"]')?.textContent?.trim() ??
            "",
          closed_text: closed?.textContent?.trim() ?? "",
          runtime_text:
            runtime?.textContent?.trim() ??
            card.querySelector('.inbox-row-status-label')?.textContent?.trim() ??
            "",
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

function observeRuntimeStateAfterStream(detail) {
  if (observerClosing) {
    return;
  }
  const targetCommitSeq = Number(detail?.catalog_commit_seq);
  const targetSessionId = String(detail?.session_id || sessionId);
  if (!Number.isFinite(targetCommitSeq) || targetCommitSeq <= 0) {
    return;
  }
  const targetKey = `${targetSessionId}:${targetCommitSeq}`;
  if (runtimeSettlementTargets.has(targetKey)) {
    return;
  }
  runtimeSettlementTargets.add(targetKey);

  runtimeSettlementChain = runtimeSettlementChain
    .then(async () => {
      if (observerClosing) {
        return;
      }
      const targetPage =
        detail?.page_pathname && detail.page_pathname !== "/timeline" ? detailPage : page;
      if (!targetPage || targetPage.isClosed()) {
        if (!observerClosing) {
          emit("runtime_state_timeout", {
            error: "target page is closed",
            detail,
            catalog_commit_seq: targetCommitSeq,
          });
        }
        return;
      }
      try {
        const handle = await targetPage.waitForFunction(
          ({ sessionId, targetCommitSeq }) => {
            const escaped = CSS.escape(sessionId);
            const card = document.querySelector(
              `[data-session-id="${escaped}"], [data-thread-id="${escaped}"]`,
            );
            if (!card) return false;
            const rawCommitSeq = card.getAttribute("data-state-commit-seq");
            const stateCommitSeq = Number(rawCommitSeq);
            if (!Number.isFinite(stateCommitSeq) || stateCommitSeq < targetCommitSeq) {
              return false;
            }
            return {
              session_id: card.getAttribute("data-session-id"),
              thread_id: card.getAttribute("data-thread-id"),
              activity_state: card.getAttribute("data-activity-state"),
              activity_observed_at: card.getAttribute("data-activity-observed-at"),
              state_commit_seq: rawCommitSeq,
              runtime_tone: card.getAttribute("data-runtime-tone") || card.getAttribute("data-status"),
              page_observed_at_wall: new Date().toISOString(),
              page_performance_now_ms: performance.now(),
              runtime_text:
                card.querySelector('[data-testid="session-card-runtime"]')?.textContent?.trim() ??
                card.querySelector('.inbox-row-status-label')?.textContent?.trim() ??
                "",
            };
          },
          { sessionId: targetSessionId, targetCommitSeq },
          { timeout: 45000, polling: "raf" },
        );
        const domMatchedElapsedMs = elapsedMs();
        const card = await handle.jsonValue();
        await handle.dispose();
        await afterPaintOn(targetPage);
        const paintStamp = await targetPage.evaluate(() => ({
          page_painted_at_wall: new Date().toISOString(),
          page_painted_performance_now_ms: performance.now(),
        }));
        emit("runtime_state_painted", {
          detail,
          dom_matched_elapsed_ms: domMatchedElapsedMs,
          stream_catalog_commit_seq: targetCommitSeq,
          card: { ...card, ...paintStamp },
        });
      } catch (error) {
        if (!observerClosing) {
          emit("runtime_state_timeout", {
            detail,
            stream_catalog_commit_seq: targetCommitSeq,
            error: String(error).slice(0, 500),
          });
        }
      }
    })
    .catch((error) => {
      if (!observerClosing) {
        emit("runtime_state_timeout", {
          detail,
          stream_catalog_commit_seq: targetCommitSeq,
          error: String(error).slice(0, 500),
        });
      }
    });
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

async function waitForDetailWorkspaceRoot(timeoutMs) {
  try {
    const handle = await detailPage.waitForFunction(
      ({ sessionId }) => {
        const root = document.querySelector(
          `.session-workspace-route[data-session-id="${CSS.escape(sessionId)}"]`,
        );
        if (!root) return false;
        return {
          session_id: root.getAttribute("data-session-id"),
          state_commit_seq: root.getAttribute("data-state-commit-seq"),
          activity_state: root.getAttribute("data-activity-state"),
          activity_observed_at: root.getAttribute("data-activity-observed-at"),
          page_observed_at_wall: new Date().toISOString(),
          page_performance_now_ms: performance.now(),
        };
      },
      { sessionId },
      { timeout: timeoutMs, polling: "raf" },
    );
    const domMatchedElapsedMs = elapsedMs();
    const root = await handle.jsonValue();
    await handle.dispose();
    await afterPaintOn(detailPage);
    const paintStamp = await detailPage.evaluate(() => ({
      page_painted_at_wall: new Date().toISOString(),
      page_painted_performance_now_ms: performance.now(),
    }));
    emit("detail_workspace_root_ready", {
      dom_matched_elapsed_ms: domMatchedElapsedMs,
      root: { ...root, ...paintStamp },
    });
    return true;
  } catch (error) {
    emit("detail_workspace_root_ready_timeout", { error: String(error).slice(0, 500) });
    return false;
  }
}

async function openDetailObserver(context) {
  detailPage = await context.newPage();
  detailPage.on("request", (request) => {
    if (request.url().includes("/workspace/stream")) {
      emit("detail_workspace_request", {
        method: request.method(),
        url: request.url(),
      });
    }
  });
  detailPage.on("response", (response) => {
    if (response.url().includes("/workspace/stream")) {
      emit("detail_workspace_response", {
        status: response.status(),
        url: response.url(),
      });
    }
  });
  detailPage.on("requestfailed", (request) => {
    if (request.url().includes("/workspace/stream")) {
      emit("detail_workspace_failed", {
        error: request.failure()?.errorText || "request failed",
        url: request.url(),
      });
    }
  });
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
  void waitForDetailWorkspaceRoot(90000);
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
  context.on("request", (request) => {
    if (request.url().includes("/telemetry/client-render")) {
      emit("client_render_beacon_request", {
        method: request.method(),
        url: request.url(),
        beacons: clientRenderBeaconPayload(request),
      });
    }
  });
  context.on("response", (response) => {
    if (response.url().includes("/telemetry/client-render")) {
      emit("client_render_beacon_response", {
        status: response.status(),
        url: response.url(),
      });
    }
  });
  context.on("requestfailed", (request) => {
    if (request.url().includes("/telemetry/client-render")) {
      emit("client_render_beacon_failed", {
        error: request.failure()?.errorText || "request failed",
        url: request.url(),
      });
    }
  });
  await context.exposeFunction("__longhouseProfilerClientRenderBeacon", (payload) => {
    if (!payload || typeof payload !== "object") return;
    emit("client_render_beacon_payload", payload);
  });
  await context.exposeFunction("__longhouseProfilerTimelineStreamEvent", (detail) => {
    if (!detail || typeof detail !== "object") {
      return;
    }
    const kind = typeof detail.kind === "string" ? detail.kind : "unknown";
    emit(`timeline_stream_${kind}`, { detail });
    if (
      kind === "workspace_changed" &&
      detail.has_transcript_preview &&
      Number(detail.transcript_preview_text_length || 0) > 0
    ) {
      emit("timeline_stream_workspace_preview_changed", { detail });
    }
    if (kind === "workspace_changed" && detail.catalog_commit_seq != null) {
      observeRuntimeStateAfterStream(detail);
    }
    if (kind === "workspace_connected" && detail.page_pathname !== "/timeline") {
      emit("detail_workspace_stream_ready", { detail });
    }
  });
  await context.addInitScript(() => {
    const reportClientRenderBeacon = (url, data) => {
      const report = (raw) => {
        try {
          const parsed = JSON.parse(raw);
          const values = Array.isArray(parsed) ? parsed : [parsed];
          window.__longhouseProfilerClientRenderBeacon?.({
            url: String(url),
            beacons: values.filter((value) => value && typeof value === "object"),
          });
        } catch {
          // Ignore non-JSON telemetry payloads.
        }
      };
      if (typeof data === "string") {
        report(data);
      } else if (data instanceof Blob) {
        void data.text().then(report);
      }
    };
    const nativeSendBeacon = navigator.sendBeacon?.bind(navigator);
    if (nativeSendBeacon) {
      navigator.sendBeacon = (url, data) => {
        if (String(url).includes("/telemetry/client-render")) {
          reportClientRenderBeacon(url, data);
        }
        return nativeSendBeacon(url, data);
      };
    }
    window.addEventListener("longhouse:timeline-stream", (event) => {
      window.__longhouseProfilerTimelineStreamEvent?.({
        ...(event.detail || {}),
        page_url: window.location.href,
        page_pathname: window.location.pathname,
      });
    });
  });
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
  url.searchParams.set("provider", provider);
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

  // Warm-live must attach the detail workspace before the provider turn. The
  // timeline card can legitimately lag archive promotion, and waiting for it
  // first would make the browser measurement include an observer-induced
  // 30-second timeout while the real workspace stream is already live.
  const cardPaintedPromise = waitForCard("card_painted", 30000);
  await openDetailObserver(context);
  const detailFirstPainted = waitForDetailTranscript("live_transcript_first_painted", 95000);
  const detailNoncePainted = waitForDetailTranscript("live_transcript_nonce_painted", 95000);
  if (exitAfterDetailTranscript) {
    await Promise.all([detailFirstPainted, detailNoncePainted]);
    // Keep the card measurement in the same run, but do not let it delay the
    // detail observer's attachment or the provider-to-browser timing.
    await cardPaintedPromise;
  } else {
    await cardPaintedPromise;
    void detailFirstPainted;
    void detailNoncePainted;
    await waitForCard("close_painted", 420000);
  }
} catch (error) {
  emit("error", { error: String(error).slice(0, 1000) });
} finally {
  observerClosing = true;
  if (browser) {
    await browser.close();
  }
}
