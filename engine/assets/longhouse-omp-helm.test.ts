import { describe, expect, it } from "bun:test";
import { mkdirSync, mkdtempSync, rmSync } from "node:fs";
import { createServer, type Socket } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
type RegisteredTool = {
  name: string;
  execute: (
    toolCallId: string,
    params: Record<string, unknown>,
    signal: AbortSignal,
    onUpdate?: unknown,
    ctx?: unknown,
  ) => Promise<unknown>;
};
const channelDir = mkdtempSync(join(tmpdir(), "omp-helm-test-"));

Object.assign(process.env, {
  LONGHOUSE_OMP_HELM_CHANNEL_PATH: join(channelDir, "channel.sock"),
  LONGHOUSE_OMP_HELM_CHANNEL_TOKEN: "omp-helm-test-token",
  LONGHOUSE_OMP_HELM_URL: "https://runtime.test",
  LONGHOUSE_COORDINATION_TOKEN: "coordination-test-token",
  LONGHOUSE_MANAGED_SESSION_ID: "omp-helm-test-session",
});
// Dynamic import is intentional: the extension validates launch-scoped identity at module load.
const channelPath = process.env.LONGHOUSE_OMP_HELM_CHANNEL_PATH!;
const {
  default: registerExtension,
  agentEndIsTerminal,
  compactAsyncJobEvidence,
  ompProviderIsIdle,
  peerLine,
} = await import("./longhouse-omp-helm");

describe("ompProviderIsIdle", () => {
  it("uses the live context before any agent_end evidence exists", () => {
    expect(ompProviderIsIdle(undefined, false)).toBe(false);
    expect(ompProviderIsIdle(undefined, true)).toBe(true);
  });

  it("keeps an explicit continuation active through a transient idle context", () => {
    expect(ompProviderIsIdle(false, false)).toBe(false);
    expect(ompProviderIsIdle(false, true)).toBe(false);
  });

  it("re-samples a terminal result from the live context", () => {
    expect(ompProviderIsIdle(true, false)).toBe(false);
    expect(ompProviderIsIdle(true, true)).toBe(true);
  });
});

describe("agentEndIsTerminal", () => {
  it("reads OMP's own final agent_end shape as terminal", () => {
    // OMP 18.2.x: emit({ type: "agent_end", messages, willContinue: decision?.willContinue })
    expect(
      agentEndIsTerminal({
        type: "agent_end",
        messages: [],
        willContinue: undefined,
      }),
    ).toBe(true);
    expect(agentEndIsTerminal({ type: "agent_end" })).toBe(true);
    expect(
      agentEndIsTerminal({ type: "agent_end", isTerminal: undefined }),
    ).toBe(true);
  });

  it("honours explicit booleans", () => {
    expect(agentEndIsTerminal({ type: "agent_end", willContinue: true })).toBe(
      false,
    );
    expect(agentEndIsTerminal({ type: "agent_end", willContinue: false })).toBe(
      true,
    );
    expect(agentEndIsTerminal({ type: "agent_end", isTerminal: true })).toBe(
      true,
    );
    expect(
      agentEndIsTerminal({
        type: "agent_end",
        isTerminal: false,
        willContinue: false,
      }),
    ).toBe(false);
  });

  it("keeps malformed present values non-terminal", () => {
    expect(agentEndIsTerminal({ type: "agent_end", willContinue: null })).toBe(
      false,
    );
    expect(agentEndIsTerminal({ type: "agent_end", isTerminal: "true" })).toBe(
      false,
    );
  });
});

describe("async job evidence", () => {
  it("keeps manager lifecycle fields and bounded task progress", () => {
    const evidence = compactAsyncJobEvidence(
      {
        partialResult: {
          details: {
            async: { state: "running", jobId: "agent-a", type: "task" },
            progress: [
              {
                id: "agent-a",
                agent: "scout",
                agentSource: "bundled",
                status: "pending",
                description: "inspect the source",
                task: "secret task text must not cross the carrier",
                currentTool: "read",
                currentToolArgs: "secret argument",
                currentToolStartMs: 1_700_000_000_100,
                toolCount: 2,
                requests: 3,
                tokens: 42,
                contextTokens: 100,
                contextWindow: 1_000,
                cost: 0.01,
                durationMs: 500,
                modelRole: "task",
                resolvedModelIdentity: "openai/gpt",
                recentOutput: ["secret output"],
                retryState: {
                  attempt: 2,
                  maxAttempts: 4,
                  delayMs: 1_000,
                  startedAtMs: 1_700_000_000_200,
                  errorMessage: "secret provider error",
                },
              },
            ],
          },
        },
      },
      {
        running: [
          {
            id: "agent-a",
            type: "task",
            status: "running",
            label: "source survey",
            startTime: 1_700_000_000_000,
            agentId: "agent-a",
          },
        ],
        recent: [
          {
            id: "bg_2",
            type: "bash",
            status: "completed",
            label: "bounded command",
            startTime: 1_699_999_999_000,
            endTime: 1_700_000_000_500,
          },
          {
            id: "bg_3",
            type: "task",
            status: "failed",
            label: "failed task",
            startTime: 1_699_999_998_000,
            endTime: 1_700_000_000_600,
            agentId: "agent-failed",
          },
          {
            id: "bg_4",
            type: "task",
            status: "cancelled",
            label: "cancelled task",
            startTime: 1_699_999_997_000,
            endTime: 1_700_000_000_700,
            agentId: "agent-cancelled",
          },
        ],
      },
      1_700_000_001_000,
    );

    expect(evidence).toMatchObject({
      async_jobs: [
        {
          id: "agent-a",
          type: "task",
          status: "running",
          agent_id: "agent-a",
          start_time: 1_700_000_000_000,
          source: "async_job_manager",
        },
        {
          id: "bg_2",
          type: "bash",
          status: "completed",
          end_time: 1_700_000_000_500,
        },
        { id: "bg_3", status: "failed" },
        { id: "bg_4", status: "cancelled" },
      ],
      task_progress: [
        {
          job_id: "agent-a",
          agent_id: "agent-a",
          status: "pending",
          current_tool: "read",
          tool_count: 2,
          requests: 3,
          tokens: 42,
          retry_state: {
            attempt: 2,
            max_attempts: 4,
            delay_ms: 1_000,
            started_at_ms: 1_700_000_000_200,
          },
        },
      ],
      async_observed_at: "2023-11-14T22:13:21.000Z",
      async_running_complete: true,
      async_jobs_source: "omp.async_job_manager",
    });
    const rows = evidence.async_jobs as Array<Record<string, unknown>>;
    expect(rows[0]).not.toHaveProperty("owner_id");
    const serialized = JSON.stringify(evidence);
    expect(serialized).not.toContain("secret task text");
    expect(serialized).not.toContain("secret argument");
    expect(serialized).not.toContain("secret output");
    expect(serialized).not.toContain("secret provider error");
  });

  it("does not promote parentSession or an ambiguous native id into a job", () => {
    expect(
      compactAsyncJobEvidence(
        {
          parentSession: "native-parent",
          id: "bg_1",
          type: "session",
        },
        undefined,
        1_700_000_001_000,
      ),
    ).toEqual({});
  });


  it("marks tool details as partial rather than a complete registry snapshot", () => {
    expect(
      compactAsyncJobEvidence(
        {
          result: {
            details: {
              async: { state: "completed", jobId: "bg_1", type: "bash" },
            },
          },
        },
        undefined,
        1_700_000_001_000,
      ),
    ).toEqual({
      async_jobs: [
        {
          id: "bg_1",
          type: "bash",
          status: "completed",
          source: "task_details",
        },
      ],
      async_jobs_source: "omp.task_tool_details",
      async_observed_at: "2023-11-14T22:13:21.000Z",
    });
  });

  it("distinguishes an authoritative empty snapshot from no snapshot", () => {
    expect(
      compactAsyncJobEvidence(
        { type: "agent_end" },
        { running: [], recent: [] },
        1_700_000_001_000,
      ),
    ).toEqual({
      async_jobs: [],
      async_running_complete: true,
      async_jobs_source: "omp.async_job_manager",
      async_observed_at: "2023-11-14T22:13:21.000Z",
    });
    expect(compactAsyncJobEvidence({ type: "agent_end" }, undefined)).toEqual(
      {},
    );
  });
});
describe("coordination tools", () => {
  it("delivers an authenticated reply to a JSON HTTP endpoint", async () => {
    const previousUrl = process.env.LONGHOUSE_OMP_HELM_URL;
    const server = Bun.serve({
      hostname: "127.0.0.1",
      port: 0,
      async fetch(request) {
        if (
          request.headers.get("X-Agents-Token") !== "coordination-test-token" ||
          request.headers.get("X-Longhouse-Session-Id") !==
            "omp-helm-test-session"
        )
          return new Response("Forbidden", { status: 403 });
        if (request.headers.get("Content-Type") !== "application/json") {
          return new Response("JSON object required", { status: 422 });
        }
        const body = await request.json();
        if (
          new URL(request.url).pathname !==
            "/api/agents/directed-inputs/7/reply" ||
          body.text !== "reply" ||
          body.client_request_id !== "reply-1"
        )
          return new Response("Invalid reply", { status: 422 });
        return Response.json({ id: 41 }, { status: 201 });
      },
    });
    try {
      process.env.LONGHOUSE_OMP_HELM_URL = server.url.origin;
      const tools = new Map<string, RegisteredTool>();
      registerExtension({
        on: () => undefined,
        registerTool: (tool: RegisteredTool) => tools.set(tool.name, tool),
      });
      const response = await tools
        .get("reply")!
        .execute(
          "reply-call",
          { input_id: 7, text: "reply", client_request_id: "reply-1" },
          new AbortController().signal,
          undefined,
          { agent: { kind: "main" } },
        );
      expect(response).toMatchObject({
        content: [{ type: "text", text: JSON.stringify({ id: 41 }) }],
      });
    } finally {
      process.env.LONGHOUSE_OMP_HELM_URL = previousUrl;
      await server.stop(true);
    }
  });

  it("refuses native child coordination before any HTTP request", async () => {
    const previousUrl = process.env.LONGHOUSE_OMP_HELM_URL;
    let requests = 0;
    const server = Bun.serve({
      hostname: "127.0.0.1",
      port: 0,
      fetch() {
        requests += 1;
        return Response.json({ accepted: true });
      },
    });
    try {
      process.env.LONGHOUSE_OMP_HELM_URL = server.url.origin;
      const tools = new Map<string, RegisteredTool>();
      registerExtension({
        on: () => undefined,
        registerTool: (tool: RegisteredTool) => tools.set(tool.name, tool),
      });
      // Native /tan clones have depth 0 and need not use the task artifact path.
      const child = {
        agent: { kind: "sub", depth: 0 },
        sessionManager: { getSessionFile: () => join(channelDir, "clone.jsonl") },
      };
      const calls: Array<[string, Record<string, unknown>]> = [
        ["peers", { repo: "/tmp/longhouse" }],
        ["search_sessions", { query: "fixture" }],
        ["tail", { session_id: "target" }],
        ["send", { session_id: "target", text: "help", client_request_id: "child-send" }],
        ["inbox", {}],
        ["reply", { input_id: 7, text: "help", client_request_id: "child-reply" }],
      ];
      for (const [name, params] of calls) {
        const response = await tools.get(name)!.execute(
          `child-${name}`,
          params,
          new AbortController().signal,
          undefined,
          child,
        );
        expect(response).toMatchObject({ isError: true });
      }
      expect(requests).toBe(0);
    } finally {
      process.env.LONGHOUSE_OMP_HELM_URL = previousUrl;
      await server.stop(true);
    }
  });

  it("asks the wall for automation peers without changing the generic wall default", async () => {
    const tools = new Map<string, RegisteredTool>();
    registerExtension({
      on: () => undefined,
      registerTool: (tool: RegisteredTool) => tools.set(tool.name, tool),
    });
    const previousFetch = globalThis.fetch;
    const calls: string[] = [];
    globalThis.fetch = (async (input) => {
      calls.push(String(input));
      return new Response(JSON.stringify({ sessions: [] }), { status: 200 });
    }) as typeof fetch;
    try {
      await tools
        .get("peers")!
        .execute(
          "tool-call-2",
          { repo: "/tmp/longhouse" },
          new AbortController().signal,
        );
    } finally {
      globalThis.fetch = previousFetch;
    }
    expect(calls[0]).toContain("include_automation=true");
  });

  it("retries a rate-limited directed input with the same idempotency key", async () => {
    const tools = new Map<string, RegisteredTool>();
    registerExtension({
      on: () => undefined,
      registerTool: (tool: RegisteredTool) => tools.set(tool.name, tool),
    });
    const previousFetch = globalThis.fetch;
    let attempts = 0;
    globalThis.fetch = (async () => {
      attempts += 1;
      if (attempts === 1)
        return new Response(JSON.stringify({ detail: "busy" }), {
          status: 429,
          headers: { "Retry-After": "0" },
        });
      return new Response(JSON.stringify({ accepted: true }), { status: 201 });
    }) as typeof fetch;
    try {
      const response = await tools.get("send")!.execute(
        "tool-call-3",
        {
          session_id: "target",
          text: "hello",
          client_request_id: "stable-request",
        },
        new AbortController().signal,
      );
      expect(response).toMatchObject({
        content: [{ text: JSON.stringify({ accepted: true }) }],
      });
    } finally {
      globalThis.fetch = previousFetch;
    }
    expect(attempts).toBe(2);
  });
});

describe("subagent sessions", () => {
  it("does not cancel a native child's own switch or branch", async () => {
    const handlers: Record<
      string,
      (event: Record<string, unknown>, ctx: unknown) => Promise<unknown>
    > = {};
    registerExtension({
      on: (name: string, handler: (typeof handlers)[string]) => {
        handlers[name] = handler;
      },
    });
    const child = {
      agent: { kind: "sub", depth: 0 },
      sessionManager: {
        getSessionId: () => "native-clone",
        getSessionFile: () => join(channelDir, "clone.jsonl"),
      },
    };
    expect(
      await handlers.session_before_switch({ type: "session_before_switch" }, child),
    ).toBeUndefined();
    expect(
      await handlers.session_before_branch({ type: "session_before_branch" }, child),
    ).toBeUndefined();
  });

  it("keeps a subagent's context off this launch's channel", async () => {
    // The reconnect test removes the shared channel directory; each test owns
    // the socket path it listens on.
    mkdirSync(channelDir, { recursive: true });
    rmSync(channelPath, { force: true });
    const frames: Record<string, unknown>[] = [];
    const frameWaiters: Array<{
      matches: (frame: Record<string, unknown>) => boolean;
      resolve: () => void;
    }> = [];
    const sockets: Socket[] = [];
    const server = createServer((socket) => {
      sockets.push(socket);
      let buffer = "";
      socket.on("data", (chunk) => {
        buffer += chunk.toString("utf8");
        let newline = buffer.indexOf("\n");
        while (newline >= 0) {
          const frame = JSON.parse(buffer.slice(0, newline));
          buffer = buffer.slice(newline + 1);
          newline = buffer.indexOf("\n");
          frames.push(frame);
          const waiting = frameWaiters.findIndex((candidate) =>
            candidate.matches(frame),
          );
          if (waiting >= 0) frameWaiters.splice(waiting, 1)[0].resolve();
          if (frame.kind === "extension_hello") {
            socket.write(
              `${JSON.stringify({ kind: "extension_ready", ok: true, connection_id: `c${sockets.length}`, lease_generation: "g1" })}\n`,
            );
          }
        }
      });
    });
    const listening = Promise.withResolvers<void>();
    server.listen(channelPath, listening.resolve);
    await listening.promise;
    const waitForFrame = (
      matches: (frame: Record<string, unknown>) => boolean,
    ) => {
      const { promise, resolve } = Promise.withResolvers<void>();
      if (frames.some(matches)) resolve();
      else frameWaiters.push({ matches, resolve });
      return promise;
    };
    const handlers: Record<
      string,
      (event: Record<string, unknown>, ctx: unknown) => Promise<unknown>
    > = {};
    const tools = new Map<string, RegisteredTool>();
    registerExtension({
      on: (name: string, handler: (typeof handlers)[string]) => {
        handlers[name] = handler;
      },
      registerTool: (tool: RegisteredTool) => tools.set(tool.name, tool),
    });
    const contextFor = (nativeId: string, file: string) => ({
      isIdle: () => false,
      sessionManager: {
        getSessionId: () => nativeId,
        getSessionFile: () => file,
      },
    });
    // OMP's own layout: a subagent session is a sibling file inside the parent
    // session's artifacts directory, and OMP fires these events from both.
    const parent = contextFor(
      "native-parent",
      join(channelDir, "parent.jsonl"),
    );
    const subagent = contextFor(
      "native-child",
      join(channelDir, "parent", "CanTabletSurvey.jsonl"),
    );
    try {
      // The handshake completes inside this call, so every frame below is
      // written to a ready channel.
      await handlers.session_start({ type: "session_start" }, parent);
      await waitForFrame((frame) => frame.kind === "session_start");
      expect(
        await handlers.session_before_switch({ type: "session_before_switch" }, subagent),
      ).toBeUndefined();
      expect(
        await handlers.session_before_branch({ type: "session_before_branch" }, subagent),
      ).toBeUndefined();

      await handlers.agent_start({ type: "agent_start" }, parent);
      await handlers.agent_end({ type: "agent_end", willContinue: true }, parent);
      await waitForFrame((frame) => frame.kind === "agent_end");

      // The main continuation's wire state must survive every child event,
      // not merely suppress frames carrying the child's native session id.
      const nativeClone = {
        ...contextFor("native-clone", join(channelDir, "clone.jsonl")),
        agent: { kind: "sub", depth: 0 },
      };
      const childEvents: Array<[string, Record<string, unknown>]> = [
        ["agent_start", { type: "agent_start" }],
        ["agent_end", { type: "agent_end", willContinue: false }],
        ["session_switch", { type: "session_switch" }],
        ["session_branch", { type: "session_branch" }],
      ];
      for (const [name, event] of childEvents) {
        const before = frames.filter((frame) => frame.kind === "message_end").length;
        await handlers[name](event, nativeClone);
        await handlers.message_end({ type: "message_end" }, parent);
        await waitForFrame(
          (frame) =>
            frame.kind === "message_end" &&
            frames.filter((candidate) => candidate.kind === "message_end").length > before,
        );
        const snapshot = frames.findLast((frame) => frame.kind === "message_end")!;
        expect(snapshot.turn_generation).toBe(1);
        expect(snapshot.agent_end_terminal).toBe(false);
      }

      // Older OMP contexts lack agent.kind; their native artifact path still
      // prevents a task child from sending as the managed parent.
      const response = await tools.get("send")!.execute(
        "legacy-child-send",
        { session_id: "target", text: "help", client_request_id: "legacy-child" },
        new AbortController().signal,
        undefined,
        subagent,
      );
      expect(response).toMatchObject({ isError: true });

      const legacyBefore = frames.filter((frame) => frame.kind === "message_end").length;
      await handlers.session_start({ type: "session_start" }, subagent);
      await handlers.agent_start({ type: "agent_start" }, subagent);
      await handlers.message_update({ type: "message_update" }, subagent);
      await handlers.message_end({ type: "message_end" }, parent);
      // One ordered channel: this frame arriving proves the subagent's were
      // never written, and that the child never replaced the connection.
      await waitForFrame(
        (frame) =>
          frame.kind === "message_end" &&
          frames.filter((candidate) => candidate.kind === "message_end").length > legacyBefore,
      );
      const legacySnapshot = frames.findLast((frame) => frame.kind === "message_end")!;
      expect(legacySnapshot.turn_generation).toBe(1);
      expect(legacySnapshot.agent_end_terminal).toBe(false);
      expect(
        frames.some((frame) => frame.native_session_id === "native-child"),
      ).toBe(false);
      expect(sockets.length).toBe(1);

      // A subagent ending is not this session ending — the channel stays up.
      await handlers.session_shutdown({ type: "session_shutdown" }, subagent);
      const moved = contextFor(
        "native-parent",
        join(channelDir, "moved.jsonl"),
      );
      await handlers.title_change({ type: "title_change" }, moved);
      await waitForFrame((frame) => frame.kind === "title_change");
      expect(sockets[0].destroyed).toBe(false);
      expect(frames.some((frame) => frame.kind === "session_shutdown")).toBe(
        false,
      );
    } finally {
      await handlers.session_shutdown({ type: "session_shutdown" }, parent);
      for (const socket of sockets) socket.destroy();
      const closed = Promise.withResolvers<void>();
      server.close(closed.resolve);
      await closed.promise;
      rmSync(channelPath, { force: true });
    }
  });
});

describe("channel reconnect", () => {
  it("sends one session_reconnect per dropped channel, not a polling loop", async () => {
    const frames: Record<string, unknown>[] = [];
    const sockets: Socket[] = [];
    const server = createServer((socket) => {
      sockets.push(socket);
      let buffer = "";
      socket.on("data", (chunk) => {
        buffer += chunk.toString("utf8");
        let newline = buffer.indexOf("\n");
        while (newline >= 0) {
          const frame = JSON.parse(buffer.slice(0, newline));
          buffer = buffer.slice(newline + 1);
          newline = buffer.indexOf("\n");
          frames.push(frame);
          if (frame.kind === "extension_hello") {
            socket.write(
              `${JSON.stringify({ kind: "extension_ready", ok: true, connection_id: `c${sockets.length}`, lease_generation: "g1" })}\n`,
            );
          }
        }
      });
    });
    await new Promise<void>((resolve) => server.listen(channelPath, resolve));
    const handlers: Record<
      string,
      (event: Record<string, unknown>, ctx: unknown) => Promise<unknown>
    > = {};
    registerExtension({
      on: (name: string, handler: (typeof handlers)[string]) => {
        handlers[name] = handler;
      },
    });
    const ctx = {
      isIdle: () => false,
      sessionManager: {
        getSessionId: () => "native-1",
        getSessionFile: () => join(channelDir, "session.jsonl"),
      },
    };
    try {
      await handlers.session_start({ type: "session_resume" }, ctx);
      sockets[0].destroy();
      await new Promise((resolve) => setTimeout(resolve, 1500));
      const reconnects = frames.filter(
        (frame) => frame.kind === "session_reconnect",
      );
      expect(sockets.length).toBe(2);
      expect(reconnects.length).toBe(1);
    } finally {
      await handlers.session_shutdown({ type: "session_shutdown" }, ctx);
      for (const socket of sockets) socket.destroy();
      await new Promise((resolve) => server.close(resolve));
      rmSync(channelDir, { recursive: true, force: true });
    }
  });
});

describe("peerLine", () => {
  it("renders one token-cheap line per peer", () => {
    const now = Date.parse("2026-10-09T18:00:00Z");
    const item = {
      session_id: "22222222-2222-2222-2222-222222222222",
      provider: "omp",
      presence_state: "running",
      last_event_at: "2026-10-09T17:55:30Z",
      summary_title: "Moving  tools\nout of\u001b[31m service pkg",
    };
    expect(peerLine(item, now)).toBe(
      "22222222-2222-2222-2222-222222222222 omp running 4m · Moving tools out of[31m service pkg",
    );
    expect(peerLine({ ...item, last_event_at: undefined, summary_title: "" }, now)).toBe(
      "22222222-2222-2222-2222-222222222222 omp running ?",
    );
  });
});

describe("busy send", () => {
  it("refuses a send while a turn runs instead of queueing it in memory", async () => {
    mkdirSync(channelDir, { recursive: true });
    const frames: Record<string, unknown>[] = [];
    const sockets: Socket[] = [];
    const server = createServer((socket) => {
      sockets.push(socket);
      let buffer = "";
      socket.on("data", (chunk) => {
        buffer += chunk.toString("utf8");
        let newline = buffer.indexOf("\n");
        while (newline >= 0) {
          const frame = JSON.parse(buffer.slice(0, newline));
          buffer = buffer.slice(newline + 1);
          newline = buffer.indexOf("\n");
          frames.push(frame);
          if (frame.kind === "extension_hello") {
            socket.write(
              `${JSON.stringify({ kind: "extension_ready", ok: true, connection_id: "c1", lease_generation: "g1" })}\n`,
            );
          }
        }
      });
    });
    await new Promise<void>((resolve) => server.listen(channelPath, resolve));
    const handlers: Record<
      string,
      (event: Record<string, unknown>, ctx: unknown) => Promise<unknown>
    > = {};
    const sent: Array<[unknown, unknown]> = [];
    registerExtension({
      on: (name: string, handler: (typeof handlers)[string]) => {
        handlers[name] = handler;
      },
      sendUserMessage: (content: unknown, options: unknown) => {
        sent.push([content, options]);
      },
    });
    let idle = false;
    const ctx = {
      isIdle: () => idle,
      sessionManager: {
        getSessionId: () => "native-1",
        getSessionFile: () => join(channelDir, "session.jsonl"),
      },
    };
    const command = async (requestId: string) => {
      const authority = [...frames]
        .reverse()
        .find((frame) => typeof frame.auth_token === "string" && frame.connection_id === "c1")!;
      sockets[0].write(
        `${JSON.stringify({
          kind: "send",
          request_id: requestId,
          text: "hello",
          auth_token: authority.auth_token,
          session_id: authority.session_id,
          native_session_id: authority.native_session_id,
          session_file: authority.session_file,
          connection_id: authority.connection_id,
          lease_generation: authority.lease_generation,
        })}\n`,
      );
      for (let attempt = 0; attempt < 50; attempt += 1) {
        const reply = frames.find(
          (frame) => frame.kind === "command_result" && frame.request_id === requestId,
        );
        if (reply) return reply;
        await new Promise((resolve) => setTimeout(resolve, 20));
      }
      throw new Error(`no command_result for ${requestId}`);
    };
    try {
      await handlers.session_start({ type: "session_resume" }, ctx);
      for (let attempt = 0; attempt < 50 && !frames.some((f) => f.connection_id === "c1"); attempt += 1)
        await new Promise((resolve) => setTimeout(resolve, 20));

      const busy = await command("busy-send");
      expect(busy.ok).toBe(false);
      expect((busy.error as Record<string, unknown>).code).toBe("turn_active");
      expect(sent.length).toBe(0);

      // The durable drain cannot carry images yet, so a busy send with
      // attachments still rides the provider's follow-up queue.
      const authority = [...frames]
        .reverse()
        .find((frame) => typeof frame.auth_token === "string" && frame.connection_id === "c1")!;
      sockets[0].write(
        `${JSON.stringify({
          kind: "send",
          request_id: "busy-image",
          text: "look at this",
          attachments: [{ mime_type: "image/png", data: "iVBORw0KGgo=" }],
          auth_token: authority.auth_token,
          session_id: authority.session_id,
          native_session_id: authority.native_session_id,
          session_file: authority.session_file,
          connection_id: authority.connection_id,
          lease_generation: authority.lease_generation,
        })}\n`,
      );
      for (let attempt = 0; attempt < 50 && !frames.some((f) => f.request_id === "busy-image"); attempt += 1)
        await new Promise((resolve) => setTimeout(resolve, 20));
      expect(sent.length).toBe(1);
      expect(sent[0][1]).toEqual({ deliverAs: "followUp" });
      sent.length = 0;

      idle = true;
      const delivered = await command("idle-send");
      expect(delivered.ok).toBe(true);
      expect(sent.length).toBe(1);
      expect(sent[0][1]).toBeUndefined();
    } finally {
      await handlers.session_shutdown({ type: "session_shutdown" }, ctx);
      for (const socket of sockets) socket.destroy();
      await new Promise((resolve) => server.close(resolve));
      rmSync(channelDir, { recursive: true, force: true });
    }
  });
});
