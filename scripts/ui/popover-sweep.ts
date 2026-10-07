/**
 * Popover containment sweep: open every menu on a rendered page and prove
 * each one is fully on screen.
 *
 * Screenshots only show a page at rest, so a menu that opens below the window
 * passes every capture; the composer's model menu did exactly that until a
 * user hit it (2026-10-06). This opens each disclosure and popup trigger in
 * turn and checks whatever it opens as an overlay (absolute or fixed
 * position) against three rules:
 *
 *   1. inside the viewport,
 *   2. on top: the element at its centre and inset corners is the overlay,
 *   3. not cut off by an ancestor that clips (overflow other than visible).
 *
 * Content a trigger expands inline (a tool row's output, a notice body) is
 * not an overlay and is not judged: it scrolls with the page.
 *
 * Used by `ui-capture --sweep`, which `make ui-sweep` runs for every fixture
 * scene at desktop, wide and phone sizes.
 */
import type { Page } from "playwright";

export type PopoverFailure = {
  trigger: string;
  overlay: string;
  rule: "viewport" | "occluded" | "clipped";
  detail: string;
};

export type PopoverSweepReport = {
  viewport: { width: number; height: number };
  triggers: number;
  overlaysChecked: number;
  /** Each trigger and how many overlays opening it produced. */
  opened: { trigger: string; overlays: string[] }[];
  skipped: string[];
  failures: PopoverFailure[];
};

// In-page helpers, evaluated from strings so esbuild's keepNames helper is not
// injected into the page (same reason as ui-capture's --probe).
const LABEL_FN = `(el) => {
  const id = el.getAttribute("data-testid");
  if (id) return "[data-testid=" + id + "]";
  const aria = el.getAttribute("aria-label");
  const text = (aria || el.textContent || "").trim().replace(/\\s+/g, " ").slice(0, 40);
  const cls = String(el.className || "").split(" ").filter(Boolean)[0];
  return el.tagName.toLowerCase() + (cls ? "." + cls : "") + (text ? ' "' + text + '"' : "");
}`;

// checkVisibility, not a box test: Chromium hides a closed <details>' content
// with content-visibility, and that content still reports a real box.
const VISIBLE_FN = `(el) => {
  if (!el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true, contentVisibilityAuto: true })) return false;
  const r = el.getBoundingClientRect();
  return r.width >= 1 && r.height >= 1;
}`;

const MARK_TRIGGERS = `(() => {
  const visible = ${VISIBLE_FN};
  const label = ${LABEL_FN};
  const nodes = document.querySelectorAll(
    'details:not([open]) > summary, [aria-haspopup]:not([aria-haspopup="false"]):not([aria-expanded="true"]), button[aria-expanded="false"]'
  );
  const out = [];
  for (const el of nodes) {
    if (el.closest("[data-sweep-trigger]")) continue;
    if (el.disabled || el.getAttribute("aria-disabled") === "true") continue;
    if (!visible(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.bottom <= 0 || r.top >= innerHeight || r.right <= 0 || r.left >= innerWidth) continue;
    el.setAttribute("data-sweep-trigger", String(out.length));
    out.push(label(el));
  }
  return out;
})()`;

const SNAPSHOT_VISIBLE = `(() => {
  const visible = ${VISIBLE_FN};
  for (const el of document.querySelectorAll("body *")) el.__sweepWasVisible = visible(el);
})()`;

const CHECK_OVERLAYS = `(() => {
  const visible = ${VISIBLE_FN};
  const label = ${LABEL_FN};
  const vw = document.documentElement.clientWidth;
  const vh = document.documentElement.clientHeight;
  const TOL = 1;
  const appeared = [];
  for (const el of document.querySelectorAll("body *")) {
    if (el.__sweepWasVisible || !visible(el)) continue;
    const pos = getComputedStyle(el).position;
    if (pos !== "absolute" && pos !== "fixed") continue;
    const r = el.getBoundingClientRect();
    if (r.width < 24 || r.height < 16) continue; // dots, carets, focus rings
    if (getComputedStyle(el).pointerEvents === "none") continue; // decoration
    appeared.push(el);
  }
  // Judge only the outermost new overlay; its children move with it.
  const roots = appeared.filter((el) => !appeared.some((other) => other !== el && other.contains(el)));
  const failures = [];
  for (const el of roots) {
    const r = el.getBoundingClientRect();
    const name = label(el);
    if (r.left < -TOL || r.top < -TOL || r.right > vw + TOL || r.bottom > vh + TOL) {
      const off = [];
      if (r.top < -TOL) off.push(Math.round(-r.top) + "px above");
      if (r.bottom > vh + TOL) off.push(Math.round(r.bottom - vh) + "px below");
      if (r.left < -TOL) off.push(Math.round(-r.left) + "px left of");
      if (r.right > vw + TOL) off.push(Math.round(r.right - vw) + "px right of");
      failures.push({ overlay: name, rule: "viewport", detail: off.join(", ") + " the window" });
      continue;
    }
    let clipped = null;
    for (let a = el.parentElement; a && a !== document.body; a = a.parentElement) {
      const c = getComputedStyle(a);
      if (c.overflowX === "visible" && c.overflowY === "visible") continue;
      if (getComputedStyle(el).position === "fixed" && c.transform === "none" && c.contain === "none") continue;
      const ar = a.getBoundingClientRect();
      if (r.left < ar.left - TOL || r.top < ar.top - TOL || r.right > ar.right + TOL || r.bottom > ar.bottom + TOL) {
        clipped = label(a);
        break;
      }
    }
    if (clipped) {
      failures.push({ overlay: name, rule: "clipped", detail: "cut off by " + clipped });
      continue;
    }
    // Sample inside the rounded corner, or the point lands on the page behind it.
    const radius = parseFloat(getComputedStyle(el).borderTopLeftRadius) || 0;
    const inset = Math.min(Math.max(4, radius), r.width / 2, r.height / 2);
    const points = [
      [r.left + r.width / 2, r.top + r.height / 2],
      [r.left + inset, r.top + inset],
      [r.right - inset, r.top + inset],
      [r.left + inset, r.bottom - inset],
      [r.right - inset, r.bottom - inset],
    ];
    for (const [x, y] of points) {
      const hit = document.elementFromPoint(x, y);
      // A drawer may cover its own scrim: overlays opened together may
      // overlap each other, never the page they opened over.
      if (hit && !roots.some((root) => root.contains(hit))) {
        failures.push({ overlay: name, rule: "occluded", detail: "covered by " + label(hit) + " at " + Math.round(x) + "," + Math.round(y) });
        break;
      }
    }
  }
  return { checked: roots.length, overlays: roots.map(label), failures };
})()`;

const CLOSE_TRIGGER = `((index) => {
  const el = document.querySelector('[data-sweep-trigger="' + index + '"]');
  if (!el) return;
  const details = el.tagName === "SUMMARY" ? el.parentElement : null;
  if (details && details.open) details.open = false;
})`;

/** shotPrefix: also save a PNG of the page with each overlay open (`<prefix>-<n>.png`). */
export async function sweepPopovers(page: Page, shotPrefix?: string): Promise<PopoverSweepReport> {
  const viewport = page.viewportSize() ?? { width: 0, height: 0 };
  const url = page.url();
  const triggers = (await page.evaluate(MARK_TRIGGERS)) as string[];
  const report: PopoverSweepReport = {
    viewport,
    triggers: triggers.length,
    overlaysChecked: 0,
    opened: [],
    skipped: [],
    failures: [],
  };

  for (let index = 0; index < triggers.length; index += 1) {
    const trigger = page.locator(`[data-sweep-trigger="${index}"]`);
    if ((await trigger.count()) === 0 || !(await trigger.isVisible())) {
      report.skipped.push(`${triggers[index]}: gone before its turn`);
      continue;
    }
    await page.evaluate(SNAPSHOT_VISIBLE);
    try {
      await trigger.click({ timeout: 2_000 });
    } catch (error) {
      report.skipped.push(`${triggers[index]}: not clickable (${String(error).split("\n")[0].slice(0, 80)})`);
      continue;
    }
    await page.waitForTimeout(200);
    if (page.url() !== url) {
      report.skipped.push(`${triggers[index]}: navigated away`);
      await page.goBack().catch(() => undefined);
      break;
    }
    const result = (await page.evaluate(CHECK_OVERLAYS)) as {
      checked: number;
      overlays: string[];
      failures: Omit<PopoverFailure, "trigger">[];
    };
    report.overlaysChecked += result.checked;
    report.opened.push({ trigger: triggers[index], overlays: result.overlays });
    if (shotPrefix && result.checked > 0) {
      await page.screenshot({ path: `${shotPrefix}-${index}.png` });
    }
    for (const failure of result.failures) report.failures.push({ trigger: triggers[index], ...failure });

    // Put the page back: Escape for menus and dialogs, then close a <details>
    // directly, then a second click for a toggle that ignores Escape.
    await page.keyboard.press("Escape");
    await page.evaluate(`${CLOSE_TRIGGER}(${index})`);
    if ((await trigger.count()) > 0 && (await trigger.getAttribute("aria-expanded")) === "true") {
      await trigger.click({ timeout: 2_000 }).catch(() => undefined);
    }
    await page.waitForTimeout(100);
  }
  return report;
}
