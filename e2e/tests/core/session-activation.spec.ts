import { randomUUID } from "crypto";
import type { APIRequestContext, Page } from "@playwright/test";
import { WebSocket } from "ws";
import { test, expect } from "../fixtures";
import { resetDatabase } from "../test-utils";

type RegisteredRunner = {
  id: number;
  name: string;
  secret: string;
};

async function registerRunner(
  request: APIRequestContext,
  name: string,
): Promise<RegisteredRunner> {
  const enrollResponse = await request.post("/api/runners/enroll-token");
  expect(
    enrollResponse.ok(),
    `runner enroll token failed: ${enrollResponse.status()} ${await enrollResponse.text()}`,
  ).toBeTruthy();
  const enrollPayload = await enrollResponse.json();

  const registerResponse = await request.post("/api/runners/register", {
    data: {
      enroll_token: enrollPayload.enroll_token,
      name,
      capabilities: ["exec.full"],
      metadata: {
        platform: "linux",
        arch: "amd64",
        hostname: name,
        install_mode: "server",
      },
    },
  });
  expect(
    registerResponse.ok(),
    `runner register failed: ${registerResponse.status()} ${await registerResponse.text()}`,
  ).toBeTruthy();

  const registerPayload = await registerResponse.json();
  return {
    id: registerPayload.runner_id,
    name: registerPayload.name,
    secret: registerPayload.runner_secret,
  };
}

async function connectRunner(
  backendUrl: string,
  workerId: string,
  runner: RegisteredRunner,
): Promise<() => Promise<void>> {
  const websocketUrl = `${backendUrl.replace(/^http/, "ws")}/api/runners/ws?worker=${encodeURIComponent(workerId)}`;
  const ws = new WebSocket(websocketUrl);

  await new Promise<void>((resolve, reject) => {
    const timeout = setTimeout(() => {
      cleanup();
      try {
        ws.close();
      } catch {
        // Ignore close failures on timed out connects.
      }
      reject(new Error(`Runner websocket timed out for ${runner.name}`));
    }, 5_000);

    const handleOpen = () => {
      try {
        ws.send(
          JSON.stringify({
            type: "hello",
            runner_id: runner.id,
            runner_name: runner.name,
            secret: runner.secret,
            metadata: {
              platform: "linux",
              arch: "amd64",
              hostname: runner.name,
              install_mode: "server",
              capabilities: ["exec.full"],
            },
          }),
        );
        cleanup();
        resolve();
      } catch (error) {
        cleanup();
        reject(error);
      }
    };

    const handleError = () => {
      cleanup();
      reject(new Error(`Runner websocket error for ${runner.name}`));
    };

    function cleanup() {
      clearTimeout(timeout);
      ws.removeEventListener("open", handleOpen);
      ws.removeEventListener("error", handleError);
    }

    ws.addEventListener("open", handleOpen);
    ws.addEventListener("error", handleError);
  });

  return async () => {
    if (ws.readyState === WebSocket.CLOSED) {
      return;
    }

    await new Promise<void>((resolve) => {
      const timeout = setTimeout(resolve, 2_000);
      ws.addEventListener(
        "close",
        () => {
          clearTimeout(timeout);
          resolve();
        },
        { once: true },
      );
      try {
        ws.close();
      } catch {
        clearTimeout(timeout);
        resolve();
      }
    });
  };
}

async function waitForRunnerOnline(
  request: APIRequestContext,
  runnerId: number,
): Promise<void> {
  await expect
    .poll(
      async () => {
        const response = await request.get("/api/runners/");
        expect(
          response.ok(),
          `runner list failed: ${response.status()} ${await response.text()}`,
        ).toBeTruthy();
        const payload = await response.json();
        const runner = payload.runners.find((item: { id: number }) => item.id === runnerId);
        return runner?.status ?? "missing";
      },
      {
        timeout: 30_000,
        message: `runner ${runnerId} never became online`,
      },
    )
    .toBe("online");
}

async function openMachinesFromTimeline(
  page: Page,
): Promise<void> {
  // Navigate directly — the Machines nav item is the primary entry point
  await page.goto("/runners");
  await page.waitForSelector('body[data-ready="true"]', { timeout: 15_000 });
}

test.describe("Session activation surfaces", () => {
  test.beforeEach(async ({ request }) => {
    await resetDatabase(request);
  });

  test("timeline keeps Machines one click away when no launch host exists", async ({
    page,
  }) => {
    await page.goto("/timeline");
    await page.waitForSelector('body[data-ready="true"]', { timeout: 15_000 });

    const runnerAction = page.getByTestId("timeline-empty-runner-action");
    const runnerActionText = await runnerAction.textContent({ timeout: 500 }).catch(() => null);
    if (runnerActionText !== null) {
      expect(runnerActionText.trim()).toBe("Machines");
    }
    const globalRunnerTab = page.getByTestId("global-runners-tab");
    await expect(globalRunnerTab).toBeVisible();
    await expect(globalRunnerTab).toHaveText("Machines");
    await globalRunnerTab.click();

    await page.waitForURL("**/runners", { timeout: 10_000 });
    await page.waitForSelector('body[data-ready="true"]', { timeout: 15_000 });
  });

  test("timeline runner action opens the machines page when a ready runner exists", async ({
    page,
    request,
    backendUrl,
    workerId,
  }) => {
    const runner = await registerRunner(request, `solo-${randomUUID().slice(0, 8)}`);
    const disconnectRunner = await connectRunner(backendUrl, workerId, runner);
    await waitForRunnerOnline(request, runner.id);

    try {
      await page.goto("/timeline");
      await page.waitForSelector('body[data-ready="true"]', { timeout: 15_000 });

      await openMachinesFromTimeline(page);

      const runnerCard = page.getByTestId(`runner-card-${runner.id}`);
      await expect(runnerCard).toBeVisible();
      await expect(runnerCard).toContainText(runner.name);
      await expect(runnerCard).toContainText("online");
    } finally {
      await disconnectRunner();
    }
  });

  test("multiple ready machines keep runner detail one click away from the machines grid", async ({
    page,
    request,
    backendUrl,
    workerId,
  }) => {
    const firstRunner = await registerRunner(request, `alpha-${randomUUID().slice(0, 8)}`);
    const secondRunner = await registerRunner(request, `beta-${randomUUID().slice(0, 8)}`);
    const disconnectFirst = await connectRunner(backendUrl, workerId, firstRunner);
    const disconnectSecond = await connectRunner(backendUrl, workerId, secondRunner);

    try {
      await waitForRunnerOnline(request, firstRunner.id);
      await waitForRunnerOnline(request, secondRunner.id);

      await page.goto("/timeline");
      await page.waitForSelector('body[data-ready="true"]', { timeout: 15_000 });

      await openMachinesFromTimeline(page);

      const secondRunnerCard = page.getByTestId(`runner-card-${secondRunner.id}`);
      await expect(secondRunnerCard).toContainText(secondRunner.name);
      await secondRunnerCard.click();

      await page.waitForURL(`**/runners/${secondRunner.id}`, { timeout: 15_000 });
      await expect(page.getByRole("heading", { name: secondRunner.name })).toBeVisible();
      await expect(page.getByRole("button", { name: /back/i })).toBeVisible();
    } finally {
      await disconnectSecond();
      await disconnectFirst();
    }
  });
});
