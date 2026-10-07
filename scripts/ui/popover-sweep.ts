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
 * Triggers are the ones that declare a popup: <details>, aria-haspopup and
 * aria-expanded. A declared trigger inside an opened menu (a menu item that
 * opens a drawer) is followed one level down, and dialogs a scene renders
 * already open are judged on arrival.
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

const TRIGGER_SELECTOR =
  'details:not([open]) > summary, [aria-haspopup]:not([aria-haspopup="false"]):not([aria-expanded="true"]), button[aria-expanded="false"]';

const MARK_TRIGGERS = `(() => {
  const visible = ${VISIBLE_FN};
  const label = ${LABEL_FN};
  const nodes = document.querySelectorAll('${TRIGGER_SELECTOR}');
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

// mode "new": overlays that appeared since SNAPSHOT_VISIBLE. mode "open":
// dialogs and menus already showing (a scene that renders one open). parent:
// mark declared triggers inside the overlays as data-sweep-child="<parent>.<n>".
const CHECK_OVERLAYS = `((mode, parent) => {
  const visible = ${VISIBLE_FN};
  const label = ${LABEL_FN};
  const vw = document.documentElement.clientWidth;
  const vh = document.documentElement.clientHeight;
  const TOL = 1;
  const appeared = [];
  const candidates = mode === "open"
    ? document.querySelectorAll('dialog[open], [role="dialog"], [role="alertdialog"], [aria-modal="true"], [role="menu"], [role="listbox"]')
    : document.querySelectorAll("body *");
  for (const el of candidates) {
    if (mode === "new" && el.__sweepWasVisible) continue;
    if (!visible(el)) continue;
    const pos = getComputedStyle(el).position;
    if (mode === "new" && pos !== "absolute" && pos !== "fixed") continue;
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
  const children = [];
  if (parent !== null) {
    for (const root of roots) {
      for (const el of root.querySelectorAll('${TRIGGER_SELECTOR}')) {
        if (el.disabled || el.getAttribute("aria-disabled") === "true" || !visible(el)) continue;
        el.setAttribute("data-sweep-child", parent + "." + children.length);
        children.push(label(el));
      }
    }
  }
  return { checked: roots.length, overlays: roots.map(label), failures, children };
})`;

const CLOSE_DETAILS = `((selector) => {
  const el = document.querySelector(selector);
  const details = el && el.tagName === "SUMMARY" ? el.parentElement : null;
  if (details && details.open) details.open = false;
})`;

// A menu's items unmount when it closes: after reopening it, find the child
// again by its label and mark it.
const REMARK_CHILD = `((childId, name) => {
  const visible = ${VISIBLE_FN};
  const label = ${LABEL_FN};
  for (const el of document.querySelectorAll('${TRIGGER_SELECTOR}')) {
    if (el.hasAttribute("data-sweep-trigger") || !visible(el) || label(el) !== name) continue;
    el.setAttribute("data-sweep-child", childId);
    return true;
  }
  return false;
})`;

type CheckResult = {
  checked: number;
  overlays: string[];
  failures: Omit<PopoverFailure, "trigger">[];
  children: string[];
};

/** shotPrefix: also save a PNG of the page with each overlay open (`<prefix>-<n>.png`). */
export async function sweepPopovers(page: Page, shotPrefix?: string): Promise<PopoverSweepReport> {
  const viewport = page.viewportSize() ?? { width: 0, height: 0 };
  const url = page.url();
  const report: PopoverSweepReport = {
    viewport,
    triggers: 0,
    overlaysChecked: 0,
    opened: [],
    skipped: [],
    failures: [],
  };
  const record = (trigger: string, result: CheckResult) => {
    report.overlaysChecked += result.checked;
    report.opened.push({ trigger, overlays: result.overlays });
    for (const failure of result.failures) report.failures.push({ trigger, ...failure });
  };

  // A dialog the scene renders open never gets a trigger click: judge it now.
  const atLoad = (await page.evaluate(`${CHECK_OVERLAYS}("open", null)`)) as CheckResult;
  if (atLoad.checked > 0) record("(open at load)", atLoad);

  // Click a trigger, judge what it opened, then put the page back.
  const openAndCheck = async (
    selector: string,
    name: string,
    parent: string | null,
    shot: string,
  ): Promise<CheckResult | null> => {
    const trigger = page.locator(selector);
    await page.evaluate(SNAPSHOT_VISIBLE);
    try {
      await trigger.click({ timeout: 2_000 });
    } catch (error) {
      report.skipped.push(`${name}: not clickable (${String(error).split("\n")[0].slice(0, 80)})`);
      return null;
    }
    await page.waitForTimeout(200);
    if (page.url() !== url) {
      report.skipped.push(`${name}: navigated away`);
      await page.goBack().catch(() => undefined);
      return null;
    }
    const result = (await page.evaluate(
      `${CHECK_OVERLAYS}("new", ${JSON.stringify(parent)})`,
    )) as CheckResult;
    record(name, result);
    if (shotPrefix && result.checked > 0) {
      await page.screenshot({ path: `${shotPrefix}-${shot}.png` });
    }
    return result;
  };
  // Escape for menus and dialogs, then close a <details> directly, then a
  // second click for a toggle that ignores Escape.
  const close = async (selector: string) => {
    await page.keyboard.press("Escape");
    await page.evaluate(`${CLOSE_DETAILS}(${JSON.stringify(selector)})`);
    const trigger = page.locator(selector);
    if ((await trigger.count()) > 0 && (await trigger.getAttribute("aria-expanded")) === "true") {
      await trigger.click({ timeout: 2_000 }).catch(() => undefined);
    }
    await page.waitForTimeout(100);
  };

  const triggers = (await page.evaluate(MARK_TRIGGERS)) as string[];
  report.triggers = triggers.length;
  for (let index = 0; index < triggers.length; index += 1) {
    const selector = `[data-sweep-trigger="${index}"]`;
    if ((await page.locator(selector).count()) === 0 || !(await page.locator(selector).isVisible())) {
      report.skipped.push(`${triggers[index]}: gone before its turn`);
      continue;
    }
    const result = await openAndCheck(selector, triggers[index], String(index), String(index));
    if (result === null) {
      if (page.url() !== url) break;
      continue;
    }
    await close(selector);

    // One level down: a declared trigger inside the menu just opened.
    report.triggers += result.children.length;
    for (let child = 0; child < result.children.length; child += 1) {
      const childSelector = `[data-sweep-child="${index}.${child}"]`;
      const childName = `${triggers[index]} > ${result.children[child]}`;
      if (!(await page.locator(childSelector).isVisible().catch(() => false))) {
        await page.locator(selector).click({ timeout: 2_000 }).catch(() => undefined);
        await page.waitForTimeout(200);
        await page.evaluate(
          `${REMARK_CHILD}(${JSON.stringify(`${index}.${child}`)}, ${JSON.stringify(result.children[child])})`,
        );
      }
      if (!(await page.locator(childSelector).isVisible().catch(() => false))) {
        report.skipped.push(`${childName}: its menu did not reopen`);
        continue;
      }
      const childResult = await openAndCheck(childSelector, childName, null, `${index}-${child}`);
      if (childResult === null && page.url() !== url) break;
      await close(childSelector);
      await close(selector);
    }
    if (page.url() !== url) break;
  }
  return report;
}
