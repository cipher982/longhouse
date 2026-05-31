import { randomUUID } from "crypto";
import type { APIRequestContext } from "@playwright/test";
import { test, expect } from "../fixtures";
import { resetDatabase } from "../test-utils";

async function mintFreshDeviceToken(
  request: APIRequestContext,
): Promise<string> {
  const response = await request.post("/api/devices/tokens", {
    data: { device_id: `playwright-mobile-${randomUUID()}` },
  });
  expect(response.ok(), await response.text()).toBe(true);

  const payload = await response.json();
  expect(typeof payload?.token).toBe("string");
  return payload.token as string;
}

async function seedSessionDetailFixture(
  request: APIRequestContext,
): Promise<string> {
  const deviceToken = await mintFreshDeviceToken(request);
  const sessionId = randomUUID();
  const startedAt = Date.now();

  const message = (index: number, role: "user" | "assistant", text: string) => ({
    role,
    content_text: text,
    timestamp: new Date(startedAt + index * 1000).toISOString(),
    source_path: "/tmp/mobile-session-detail.jsonl",
    source_offset: index,
  });

  const toolCall = (
    index: number,
    toolCallId: string,
    command: string,
    output: string,
  ) => [
    {
      role: "assistant",
      tool_name: "Bash",
      tool_input_json: { command },
      tool_call_id: toolCallId,
      timestamp: new Date(startedAt + index * 1000).toISOString(),
      source_path: "/tmp/mobile-session-detail.jsonl",
      source_offset: index,
    },
    {
      role: "tool",
      tool_name: "Bash",
      tool_output_text: output,
      tool_call_id: toolCallId,
      timestamp: new Date(startedAt + (index + 1) * 1000).toISOString(),
      source_path: "/tmp/mobile-session-detail.jsonl",
      source_offset: index + 1,
    },
  ];

  const events = [
    message(
      0,
      "user",
      "Audit the mobile timeline session detail route. The current phone layout feels broken and unreadable.",
    ),
    message(
      1,
      "assistant",
      "I will inspect the workspace shell, the context pane, and the timeline transcript before changing the layout.",
    ),
    ...toolCall(
      2,
      "toolu_mobile_workspace_ls",
      "ls -la web/src/components/session-workspace",
      "total 48\ndrwxr-xr-x  6 agent  staff   192 Mar 18 10:00 .\ndrwxr-xr-x 12 agent  staff   384 Mar 18 10:00 ..\n-rw-r--r--  1 agent  staff  4512 Mar 18 10:00 EventInspectorPane.tsx\n-rw-r--r--  1 agent  staff  9238 Mar 18 10:00 SessionContextPane.tsx\n-rw-r--r--  1 agent  staff  8384 Mar 18 10:00 TimelinePane.tsx\n",
    ),
    message(
      4,
      "assistant",
      "The context pane is dense, the header has multiple filter controls, and the dock composer remains mounted for Claude sessions.",
    ),
    ...toolCall(
      5,
      "toolu_mobile_css_rg",
      "rg -n \"workspace-shell|@media|timeline-pane\" web/src/styles/session-workspace.css",
      "15:.workspace-shell {\n33:.workspace-shell__body {\n522:.timeline-pane__header {\n694:.timeline-pane__list {\n1010:@media (max-width: 1180px) {\n1034:@media (max-width: 900px) {\n",
    ),
    message(
      7,
      "user",
      "Make the transcript the first-class surface on phones. I still need the context and any selected tool details, just not at the cost of readability.",
    ),
    message(
      8,
      "assistant",
      "Understood. I’ll prioritize the timeline, keep the supporting panes accessible, and avoid a desktop-only three-pane stack on mobile widths.",
    ),
    ...toolCall(
      9,
      "toolu_mobile_css_sed",
      "sed -n '1000,1085p' web/src/styles/session-workspace.css",
      "@media (max-width: 900px) {\n  .workspace-shell__body {\n    grid-template-columns: 1fr;\n    grid-template-rows: auto minmax(360px, 1fr) minmax(280px, auto);\n    grid-template-areas:\n      \"sidebar\"\n      \"main\"\n      \"inspector\";\n  }\n}\n",
    ),
    message(
      11,
      "assistant",
      "That mobile breakpoint still leads with the sidebar and keeps a heavyweight stacked shell. On smaller devices, the transcript lands too far down and the route becomes hard to scan.",
    ),
    message(
      12,
      "assistant",
      "I’ll restructure the mobile order so the transcript appears first, then selected details, then session context, while preserving desktop behavior.",
    ),
  ];

  const ingest = await request.post("/api/agents/ingest", {
    headers: {
      "X-Agents-Token": deviceToken,
    },
    data: {
      id: sessionId,
      provider: "claude",
      environment: "development",
      project: "mobile-session-detail-e2e",
      device_id: "e2e-mobile-device",
      cwd: "/Users/example/git/zerg/web/src/components/session-workspace",
      git_repo: "git@github.com:cipher982/longhouse.git",
      git_branch: "fix/mobile-session-layout-readability",
      provider_session_id: `claude-session-${sessionId}`,
      started_at: new Date(startedAt).toISOString(),
      ended_at: new Date(startedAt + 13_000).toISOString(),
      events,
    },
  });

  const ingestBody = await ingest.text();
  expect(ingest.ok(), ingestBody).toBe(true);
  return sessionId;
}

async function saveScreenshot(
  page: import("@playwright/test").Page,
  testInfo: import("@playwright/test").TestInfo,
  name: string,
): Promise<void> {
  const path = testInfo.outputPath(name);
  await page.screenshot({ path, fullPage: false });
  await testInfo.attach(name, { path, contentType: "image/png" });
}

test.beforeEach(async ({ request }) => {
  await resetDatabase(request);
});

test("session detail keeps the transcript readable on mobile", async ({
  page,
  request,
}, testInfo) => {
  const sessionId = await seedSessionDetailFixture(request);

  await page.goto(`/timeline/${sessionId}`);
  await page.waitForSelector('body[data-ready="true"]', { timeout: 10000 });
  await page.evaluate(() => {
    window.localStorage.removeItem("zerg:session-workspace-layout:v1");
  });
  await page.reload();
  await page.waitForSelector('body[data-ready="true"]', { timeout: 10000 });

  const viewport = page.viewportSize();
  if (!viewport) {
    throw new Error("Mobile viewport was not configured for the test.");
  }

  const mainPane = page.locator(".workspace-shell__pane--main");
  const timelinePane = page.getByTestId("session-timeline-pane");
  const timelineList = page.getByTestId("session-timeline-list");
  const contextPane = page.locator(".workspace-shell__pane--sidebar");

  await expect(timelinePane).toBeVisible();
  await expect(timelineList).toBeVisible();
  await saveScreenshot(page, testInfo, "session-detail-mobile-top.png");

  const horizontalOverflow = await page.evaluate(
    () => document.documentElement.scrollWidth - window.innerWidth,
  );
  expect(horizontalOverflow).toBeLessThanOrEqual(1);

  const mainBox = await mainPane.boundingBox();
  const listBox = await timelineList.boundingBox();
  const contextBox = await contextPane.boundingBox();
  const debugLayout = JSON.stringify({
    viewport,
    horizontalOverflow,
    mainBox,
    listBox,
    contextBox,
  });

  expect(mainBox, debugLayout).toBeTruthy();
  expect(listBox, debugLayout).toBeTruthy();
  expect(contextBox, debugLayout).toBeTruthy();

  expect(mainBox?.y ?? Number.POSITIVE_INFINITY, debugLayout).toBeLessThan(
    viewport.height * 0.35,
  );
  expect(listBox?.height ?? 0, debugLayout).toBeGreaterThan(
    viewport.height * 0.28,
  );
  expect(contextBox?.y ?? 0, debugLayout).toBeGreaterThan(
    (mainBox?.y ?? 0) + 80,
  );

  // Inspector pane removed; tool rows expand in-place now.
  const firstTool = page.locator('[data-row-kind="tool"]').first();
  await firstTool.click();
  await expect(firstTool).toHaveClass(/is-expanded/);
  await firstTool.scrollIntoViewIfNeeded();
  await saveScreenshot(page, testInfo, "session-detail-mobile-tool-selected.png");
});
