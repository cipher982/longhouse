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
  ompProviderIsIdle,
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
describe("coordination tools", () => {
  it("registers the peer tools and sends with session-scoped authority", async () => {
    const tools = new Map<string, RegisteredTool>();
    registerExtension({
      on: () => undefined,
      registerTool: (tool: RegisteredTool) => tools.set(tool.name, tool),
    });

    expect([...tools.keys()]).toEqual([
      "peers",
      "search_sessions",
      "tail",
      "send",
      "inbox",
      "reply",
    ]);

    const calls: Array<{ url: string; init: RequestInit }> = [];
    const previousFetch = globalThis.fetch;
    globalThis.fetch = (async (input, init) => {
      calls.push({ url: String(input), init: init ?? {} });
      return new Response(JSON.stringify({ accepted: true }), {
        status: 201,
        headers: { "content-type": "application/json" },
      });
    }) as typeof fetch;
    try {
      const response = await tools
        .get("send")!
        .execute(
          "tool-call-1",
          {
            session_id: "target",
            text: "hello",
            client_request_id: "request-1",
          },
          new AbortController().signal,
        );
      expect(response).toMatchObject({
        content: [{ type: "text", text: JSON.stringify({ accepted: true }) }],
      });
    } finally {
      globalThis.fetch = previousFetch;
    }

    expect(calls[0]?.url).toBe(
      "https://runtime.test/api/agents/directed-inputs",
    );
    expect(calls[0]?.init.headers).toMatchObject({
      "X-Agents-Token": "coordination-test-token",
      "X-Longhouse-Session-Id": "omp-helm-test-session",
    });
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
      const response = await tools
        .get("send")!
        .execute(
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
    const waitForFrame = (matches: (frame: Record<string, unknown>) => boolean) => {
      const { promise, resolve } = Promise.withResolvers<void>();
      if (frames.some(matches)) resolve();
      else frameWaiters.push({ matches, resolve });
      return promise;
    };
    const handlers: Record<
      string,
      (event: Record<string, unknown>, ctx: unknown) => Promise<unknown>
    > = {};
    registerExtension({
      on: (name: string, handler: (typeof handlers)[string]) => {
        handlers[name] = handler;
      },
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
    const parent = contextFor("native-parent", join(channelDir, "parent.jsonl"));
    const subagent = contextFor(
      "native-child",
      join(channelDir, "parent", "CanTabletSurvey.jsonl"),
    );
    try {
      // The handshake completes inside this call, so every frame below is
      // written to a ready channel.
      await handlers.session_start({ type: "session_start" }, parent);
      await waitForFrame((frame) => frame.kind === "session_start");

      await handlers.session_start({ type: "session_start" }, subagent);
      await handlers.agent_start({ type: "agent_start" }, subagent);
      await handlers.message_update({ type: "message_update" }, subagent);
      await handlers.message_end({ type: "message_end" }, parent);
      // One ordered channel: this frame arriving proves the subagent's were
      // never written, and that the child never replaced the connection.
      await waitForFrame((frame) => frame.kind === "message_end");
      expect(
        frames.some((frame) => frame.native_session_id === "native-child"),
      ).toBe(false);
      expect(sockets.length).toBe(1);

      // A subagent ending is not this session ending — the channel stays up.
      await handlers.session_shutdown({ type: "session_shutdown" }, subagent);
      const moved = contextFor("native-parent", join(channelDir, "moved.jsonl"));
      await handlers.title_change({ type: "title_change" }, moved);
      await waitForFrame((frame) => frame.kind === "title_change");
      expect(sockets[0].destroyed).toBe(false);
      expect(frames.some((frame) => frame.kind === "session_shutdown")).toBe(false);
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
