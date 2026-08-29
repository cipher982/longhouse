import { randomUUID } from "crypto";
import type { APIRequestContext } from "@playwright/test";
import { WebSocket } from "ws";
import { test, expect } from "../fixtures";
import { resetDatabase } from "../test-utils";

type MachineCommand = {
  type: "command";
  command_id: string;
  session_id: string;
  command_type: string;
  payload: Record<string, unknown>;
};

async function enrollMachine(
  request: APIRequestContext,
  deviceId: string,
): Promise<string> {
  const response = await request.post("/api/devices/tokens", {
    data: { device_id: deviceId },
  });
  expect(response.ok(), await response.text()).toBe(true);
  const body = await response.json();
  return body.token;
}

async function connectConsoleMachine(
  backendUrl: string,
  workerId: string,
  deviceId: string,
  token: string,
): Promise<{ commands: MachineCommand[]; close: () => Promise<void> }> {
  const url = `${backendUrl.replace(/^http/, "ws")}/api/agents/control/ws?worker=${encodeURIComponent(workerId)}`;
  const socket = new WebSocket(url, { headers: { "X-Agents-Token": token } });
  const commands: MachineCommand[] = [];

  socket.on("message", (data) => {
    const message = JSON.parse(data.toString());
    if (message.type !== "command") return;
    commands.push(message as MachineCommand);
    socket.send(
      JSON.stringify({
        type: "command_result",
        command_id: message.command_id,
        ok: true,
        result: { accepted: true },
      }),
    );
  });

  await new Promise<void>((resolve, reject) => {
    const timeout = setTimeout(
      () => reject(new Error("machine control websocket timed out")),
      5_000,
    );
    socket.once("error", (error) => {
      clearTimeout(timeout);
      reject(error);
    });
    socket.once("open", () => {
      clearTimeout(timeout);
      socket.send(
        JSON.stringify({
          type: "hello",
          device_id: deviceId,
          machine_name: deviceId,
          engine_build: "console-lifecycle-e2e",
          supports: ["codex.turn_start"],
        }),
      );
      resolve();
    });
  });

  return {
    commands,
    close: async () => {
      if (socket.readyState === WebSocket.CLOSED) return;
      await new Promise<void>((resolve) => {
        const timeout = setTimeout(resolve, 2_000);
        socket.once("close", () => {
          clearTimeout(timeout);
          resolve();
        });
        socket.close();
      });
    },
  };
}

async function publishRuntimeEvent(
  request: APIRequestContext,
  token: string,
  event: Record<string, unknown>,
): Promise<void> {
  const response = await request.post("/api/agents/runtime/events/batch", {
    headers: { "X-Agents-Token": token },
    data: { events: [event] },
  });
  expect(response.ok(), await response.text()).toBe(true);
}

async function readSession(request: APIRequestContext, sessionId: string) {
  const response = await request.get(`/api/timeline/sessions/${sessionId}`);
  expect(response.ok(), await response.text()).toBe(true);
  return response.json();
}

test.describe("Console lifecycle contract", () => {
  test.beforeEach(async ({ request }) => {
    await resetDatabase(request);
  });

  test("empty, active, queued, and terminal states remain live-control facts", async ({
    request,
    backendUrl,
    workerId,
  }) => {
    const deviceId = `console-${randomUUID().slice(0, 8)}`;
    const token = await enrollMachine(request, deviceId);
    const machine = await connectConsoleMachine(
      backendUrl,
      workerId,
      deviceId,
      token,
    );

    try {
      await expect
        .poll(async () => {
          const response = await request.get("/api/timeline/machines");
          const body = await response.json();
          return body.machines?.find(
            (item: { device_id: string }) => item.device_id === deviceId,
          )?.control_channel_status;
        })
        .toBe("connected");

      const createdResponse = await request.post("/api/sessions/console", {
        data: {
          device_id: deviceId,
          provider: "codex",
          cwd: "/workspace/console-e2e",
          project: "console-lifecycle-e2e",
          launch_surface: "web",
        },
      });
      expect(createdResponse.status(), await createdResponse.text()).toBe(201);
      const created = await createdResponse.json();

      const empty = await readSession(request, created.session_id);
      expect(empty.session_state.mode).toBe("console");
      expect(empty.session_state.transcript.convergence).toBe("current");
      expect(empty.session_state.presentation.primary.key).toBe("ready");
      expect(
        empty.session_state.presentation.access,
        JSON.stringify(empty.session_state, null, 2),
      ).not.toBeNull();
      expect(empty.session_state.presentation.access.key).toBe("live_control");
      expect(empty.session_state.control.actions.start_turn.state).toBe(
        "available",
      );

      const firstResponse = await request.post(
        `/api/sessions/${created.session_id}/input`,
        {
          data: {
            text: "first",
            intent: "auto",
            client_request_id: "console-e2e-first",
          },
        },
      );
      expect(firstResponse.ok(), await firstResponse.text()).toBe(true);
      const first = await firstResponse.json();
      expect(first.outcome).toBe("sent");

      await expect.poll(() => machine.commands.length).toBe(1);
      const firstCommand = machine.commands[0];
      expect(firstCommand.command_type).toBe("session.turn.start");
      const firstRunId = String(firstCommand.payload.run_id);

      await publishRuntimeEvent(request, token, {
        runtime_key: `codex:${created.session_id}`,
        session_id: created.session_id,
        thread_id: created.thread_id,
        run_id: firstRunId,
        provider: "codex",
        device_id: deviceId,
        source: "codex_exec",
        kind: "phase_signal",
        phase: "thinking",
        occurred_at: new Date().toISOString(),
        freshness_ms: 90_000,
        dedupe_key: `thinking:${firstRunId}`,
        payload: {},
      });

      const active = await readSession(request, created.session_id);
      expect(["thinking", "executing"]).toContain(
        active.session_state.activity.state,
      );
      expect(active.session_state.presentation.access.key).toBe("live_control");
      expect(active.session_state.control.actions.start_turn.state).toBe(
        "available",
      );
      expect(active.session_state.control.actions.interrupt).toEqual({
        state: "unavailable",
        reason: "unsupported",
      });
      expect(active.session_state.working_set).toBe("open");

      const secondResponse = await request.post(
        `/api/sessions/${created.session_id}/input`,
        {
          data: {
            text: "second",
            intent: "auto",
            client_request_id: "console-e2e-second",
          },
        },
      );
      expect(secondResponse.ok(), await secondResponse.text()).toBe(true);
      const second = await secondResponse.json();
      expect(second.outcome).toBe("queued");
      expect(machine.commands).toHaveLength(1);

      await publishRuntimeEvent(request, token, {
        runtime_key: `codex:${created.session_id}`,
        session_id: created.session_id,
        thread_id: created.thread_id,
        run_id: firstRunId,
        provider: "codex",
        device_id: deviceId,
        source: "codex_exec",
        kind: "terminal_signal",
        occurred_at: new Date().toISOString(),
        dedupe_key: `terminal:${firstRunId}`,
        payload: { terminal_state: "run_completed", exit_code: 0 },
      });

      await expect.poll(() => machine.commands.length).toBe(2);
      const secondRunId = String(machine.commands[1].payload.run_id);
      await expect
        .poll(async () => {
          const session = await readSession(request, created.session_id);
          return session.session_state.run?.id;
        })
        .toBe(secondRunId);
      await publishRuntimeEvent(request, token, {
        runtime_key: `codex:${created.session_id}`,
        session_id: created.session_id,
        thread_id: created.thread_id,
        run_id: secondRunId,
        provider: "codex",
        device_id: deviceId,
        source: "codex_exec",
        kind: "terminal_signal",
        occurred_at: new Date().toISOString(),
        dedupe_key: `terminal:${secondRunId}`,
        payload: { terminal_state: "run_completed", exit_code: 0 },
      });

      const terminal = await readSession(request, created.session_id);
      expect(terminal.session_state.run.lifecycle).toBe("ended");
      expect(terminal.session_state.presentation.primary.key).toBe("ended");
      expect(terminal.session_state.presentation.access.key).toBe(
        "live_control",
      );
      expect(terminal.session_state.control.actions.start_turn.state).toBe(
        "available",
      );
    } finally {
      await machine.close();
    }
  });
});
