import { describe, expect, it } from "bun:test";
import { mkdtempSync, rmSync } from "node:fs";
import { createServer, type Socket } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";

const identityKeys = [
  "LONGHOUSE_OMP_HELM_CHANNEL_PATH",
  "LONGHOUSE_OMP_HELM_CHANNEL_TOKEN",
  "LONGHOUSE_MANAGED_SESSION_ID",
] as const;
const channelDir = mkdtempSync(join(tmpdir(), "omp-helm-test-"));
const previousIdentity = Object.fromEntries(identityKeys.map((key) => [key, process.env[key]]));
Object.assign(process.env, {
  LONGHOUSE_OMP_HELM_CHANNEL_PATH: join(channelDir, "channel.sock"),
  LONGHOUSE_OMP_HELM_CHANNEL_TOKEN: "omp-helm-test-token",
  LONGHOUSE_MANAGED_SESSION_ID: "omp-helm-test-session",
});
// Dynamic import is intentional: the extension validates launch-scoped identity at module load.
const channelPath = process.env.LONGHOUSE_OMP_HELM_CHANNEL_PATH!;
const { default: registerExtension, agentEndIsTerminal, ompProviderIsIdle } = await import("./longhouse-omp-helm");
for (const key of identityKeys) {
  const value = previousIdentity[key];
  if (value === undefined) delete process.env[key];
  else process.env[key] = value;
}

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
    expect(agentEndIsTerminal({ type: "agent_end", messages: [], willContinue: undefined })).toBe(true);
    expect(agentEndIsTerminal({ type: "agent_end" })).toBe(true);
    expect(agentEndIsTerminal({ type: "agent_end", isTerminal: undefined })).toBe(true);
  });

  it("honours explicit booleans", () => {
    expect(agentEndIsTerminal({ type: "agent_end", willContinue: true })).toBe(false);
    expect(agentEndIsTerminal({ type: "agent_end", willContinue: false })).toBe(true);
    expect(agentEndIsTerminal({ type: "agent_end", isTerminal: true })).toBe(true);
    expect(agentEndIsTerminal({ type: "agent_end", isTerminal: false, willContinue: false })).toBe(false);
  });

  it("keeps malformed present values non-terminal", () => {
    expect(agentEndIsTerminal({ type: "agent_end", willContinue: null })).toBe(false);
    expect(agentEndIsTerminal({ type: "agent_end", isTerminal: "true" })).toBe(false);
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
            socket.write(`${JSON.stringify({ kind: "extension_ready", ok: true, connection_id: `c${sockets.length}`, lease_generation: "g1" })}\n`);
          }
        }
      });
    });
    await new Promise<void>((resolve) => server.listen(channelPath, resolve));
    const handlers: Record<string, (event: Record<string, unknown>, ctx: unknown) => Promise<unknown>> = {};
    registerExtension({ on: (name: string, handler: (typeof handlers)[string]) => { handlers[name] = handler; } });
    const ctx = {
      isIdle: () => false,
      sessionManager: { getSessionId: () => "native-1", getSessionFile: () => join(channelDir, "session.jsonl") },
    };
    try {
      await handlers.session_start({ type: "session_resume" }, ctx);
      sockets[0].destroy();
      await new Promise((resolve) => setTimeout(resolve, 1500));
      const reconnects = frames.filter((frame) => frame.kind === "session_reconnect");
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
