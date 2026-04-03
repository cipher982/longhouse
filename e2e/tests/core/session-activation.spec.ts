import { randomUUID } from "crypto";
import type { APIRequestContext } from "@playwright/test";
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
  commisId: string,
  runner: RegisteredRunner,
): Promise<() => Promise<void>> {
  const websocketUrl = `${backendUrl.replace(/^http/, "ws")}/api/runners/ws?commis=${encodeURIComponent(commisId)}`;
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
        timeout: 10_000,
        message: `runner ${runnerId} never became online`,
      },
    )
    .toBe("online");
}

test.describe("Session activation surfaces", () => {
  test.beforeEach(async ({ request }) => {
    await resetDatabase(request);
  });

  test("empty timeline points new users to machine setup when no launch host exists", async ({
    page,
  }) => {
    await page.goto("/timeline");
    await page.waitForSelector('body[data-ready="true"]', { timeout: 15_000 });

    const runnerAction = page.getByTestId("timeline-empty-runner-action");
    await expect(runnerAction).toHaveText("Connect Machine");
    await runnerAction.click();

    const modal = page.getByTestId("add-runner-modal");
    await expect(modal).toBeVisible();
    await expect(page.getByTestId("add-runner-command")).toContainText("/api/runners/install.sh");
  });

  test("timeline empty state opens launch directly when exactly one ready runner exists", async ({
    page,
    request,
    backendUrl,
    commisId,
  }) => {
    const runner = await registerRunner(request, `solo-${randomUUID().slice(0, 8)}`);
    const disconnectRunner = await connectRunner(backendUrl, commisId, runner);
    await waitForRunnerOnline(request, runner.id);

    let launchBody: Record<string, unknown> | null = null;
    await page.route("**/api/sessions/managed-local", async (route) => {
      launchBody = route.request().postDataJSON() as Record<string, unknown>;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          session_id: randomUUID(),
          provider: "claude",
          provider_session_id: `provider-${randomUUID()}`,
          execution_home: "managed_local",
          managed_transport: "claude_desktop_mcp",
          loop_mode: "manual",
          source_runner_id: runner.id,
          source_runner_name: runner.name,
          managed_session_name: `lh-${runner.name}`,
          attach_command: `ssh ${runner.name}`,
        }),
      });
    });

    try {
      await page.goto("/timeline");
      await page.waitForSelector('body[data-ready="true"]', { timeout: 15_000 });

      const runnerAction = page.getByTestId("timeline-empty-runner-action");
      await expect(runnerAction).toHaveText("Start Session");
      await runnerAction.click();

      const modal = page.getByTestId("launch-session-modal");
      await expect(modal).toBeVisible();

      await page.locator("#launch-cwd").fill("/Users/davidrose/git/zerg");
      await page.locator("#launch-project").fill("zerg");
      await page.locator("#launch-display-name").fill("Activation E2E");
      await page.getByRole("button", { name: "Launch" }).click();

      await expect(modal).toContainText(`Session started on ${runner.name}`);
      await expect(modal).toContainText(`ssh ${runner.name}`);
      expect(launchBody).toMatchObject({
        runner_target: `runner:${runner.id}`,
        cwd: "/Users/davidrose/git/zerg",
        provider: "claude",
        project: "zerg",
        display_name: "Activation E2E",
      });
    } finally {
      await disconnectRunner();
    }
  });

  test("multiple ready machines send users to the machines grid, where launch stays one click away", async ({
    page,
    request,
    backendUrl,
    commisId,
  }) => {
    const firstRunner = await registerRunner(request, `alpha-${randomUUID().slice(0, 8)}`);
    const secondRunner = await registerRunner(request, `beta-${randomUUID().slice(0, 8)}`);
    const disconnectFirst = await connectRunner(backendUrl, commisId, firstRunner);
    const disconnectSecond = await connectRunner(backendUrl, commisId, secondRunner);

    try {
      await waitForRunnerOnline(request, firstRunner.id);
      await waitForRunnerOnline(request, secondRunner.id);

      await page.goto("/timeline");
      await page.waitForSelector('body[data-ready="true"]', { timeout: 15_000 });

      const runnerAction = page.getByTestId("timeline-empty-runner-action");
      await expect(runnerAction).toHaveText("Choose Machine");
      await runnerAction.click();

      await page.waitForURL("**/runners", { timeout: 15_000 });
      await page.waitForSelector('body[data-ready="true"]', { timeout: 15_000 });

      const secondRunnerCard = page.getByTestId(`runner-card-${secondRunner.id}`);
      await expect(secondRunnerCard).toContainText(secondRunner.name);
      await secondRunnerCard.getByTestId(`runner-launch-button-${secondRunner.id}`).click();

      const modal = page.getByTestId("launch-session-modal");
      await expect(modal).toBeVisible();
      await expect(modal).toContainText(`Start a session on ${secondRunner.name}.`);
    } finally {
      await disconnectSecond();
      await disconnectFirst();
    }
  });
});
