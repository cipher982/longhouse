import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";

// Explicit hidden campaign sessions, never arbitrary personal history. This
// consumes real served data; it does not route/mock any Runtime Host response.
type FidelityCase = { name: string; session_id: string; markers: string[] };
const manifestPath = process.env.LONGHOUSE_FIDELITY_CASES_PATH;
function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function parseCases(value: unknown): FidelityCase[] {
  if (!Array.isArray(value) || value.length === 0) throw new Error("Fidelity manifest must contain real sessions");
  const entries: unknown[] = value;
  return entries.map(item => {
    if (!record(item) || typeof item.name !== "string" || !item.name
        || typeof item.session_id !== "string" || !item.session_id
        || !Array.isArray(item.markers) || !item.markers.length) {
      throw new Error("Each fidelity case requires a name, session_id and markers");
    }
    const markers = item.markers.map((marker: unknown) => {
      if (typeof marker !== "string" || !marker.trim()) throw new Error("Markers must be nonempty strings");
      return marker;
    });
    if (new Set(markers).size !== markers.length) throw new Error("Markers must be distinct");
    return { name: item.name, session_id: item.session_id, markers };
  });
}

function assistantRows(value: unknown, sessionId: string): { id: string; text: string }[] {
  if (!record(value) || !record(value.session) || value.session.id !== sessionId
      || (value.session.hidden_from_default_timeline !== true && value.session.launch_surface !== "test")
      || !record(value.projection) || !Array.isArray(value.projection.items)) {
    throw new Error("Workspace must contain the selected hidden or test-declared campaign session");
  }
  const items: unknown[] = value.projection.items;
  return items.flatMap(item => {
    if (!record(item) || item.kind !== "event" || !record(item.event)) return [];
    const event = item.event;
    if (event.role !== "assistant" || event.tool_name || typeof event.content_text !== "string") return [];
    if (typeof event.id !== "string") throw new Error("Assistant event has no stable identity");
    return [{ id: event.id, text: event.content_text.trim() }];
  });
}

const cases = manifestPath ? parseCases(JSON.parse(readFileSync(manifestPath, "utf8"))) : [];

for (const item of cases) {
  test(`${item.name}: cold open and return preserve ordered final replies`, async ({ page }, testInfo) => {
    const phases: Record<string, unknown>[] = [];
    const report: Record<string, unknown> = { session_id: item.session_id, polling_ms: 100, phases };
    try {
      const response = await page.request.get(`/api/timeline/sessions/${item.session_id}/workspace`);
      expect(response.ok(), "Real workspace must be readable").toBeTruthy();
      const rows = assistantRows(await response.json(), item.session_id);
      const expected = item.markers;
      const matched = rows.filter(row => expected.includes(row.text));
      expect(matched.map(row => row.text), "Served final replies must occur once and in source order").toEqual(expected);
      report.served_event_ids = matched.map(row => row.id);

      for (const phase of ["cold-open", "return-from-timeline"]) {
        const started = performance.now();
        if (phase === "cold-open") {
          await page.goto(`/sessions/${item.session_id}`);
        } else {
          await page.goBack();
        }
        let observation = 0;
        let matchedSince: number | undefined;
        await expect.poll(async () => {
          observation += 1;
          const rendered = await page.locator('[data-message-role="assistant"] .tl-msg__body').allInnerTexts();
          const markers = rendered.map(text => text.trim()).filter(text => expected.includes(text));
          phases.push({ phase, observation, elapsed_ms: Math.round(performance.now() - started), markers });
          const matches = markers.length === expected.length && markers.every((marker, index) => marker === expected[index]);
          if (matches) matchedSince ??= performance.now();
          else matchedSince = undefined;
          return matches && performance.now() - (matchedSince ?? performance.now()) >= 1_000;
        }, { timeout: 30_000, intervals: [100], message: "Actual rendered assistant replies must remain exact and ordered" }).toBe(true);
        await expect(page.locator('[data-message-role="assistant"] .tl-msg__body')
          .filter({ hasText: expected[expected.length - 1] })).toBeInViewport({ ratio: 1 });
        await testInfo.attach(`${phase}-rendered`, { body: await page.screenshot({ fullPage: true }), contentType: "image/png" });
        if (phase === "cold-open") {
          await page.getByRole("button", { name: "Timeline", exact: true }).click();
          await expect(page).toHaveURL(/\/timeline/);
        }
      }
      report.status = "pass";
    } catch (error) {
      report.status = "fail";
      report.error = String(error);
      await testInfo.attach("failure-rendered", { body: await page.screenshot({ fullPage: true }), contentType: "image/png" });
      throw error;
    } finally {
      await testInfo.attach("terminal-fidelity", { body: JSON.stringify(report, null, 2), contentType: "application/json" });
    }
  });
}
