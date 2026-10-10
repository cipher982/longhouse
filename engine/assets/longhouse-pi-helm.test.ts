import { describe, expect, it } from "bun:test";
import { mkdtempSync } from "node:fs";
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
  ) => Promise<{ content: Array<{ text: string }>; isError?: boolean }>;
};

const channelDir = mkdtempSync(join(tmpdir(), "pi-helm-test-"));
Object.assign(process.env, {
  LONGHOUSE_PI_HELM_CHANNEL_PATH: join(channelDir, "channel.sock"),
  LONGHOUSE_PI_HELM_CHANNEL_TOKEN: "pi-helm-test-token",
  LONGHOUSE_PI_HELM_URL: "https://runtime.test",
  LONGHOUSE_MANAGED_SESSION_ID: "pi-helm-test-session",
});
// Dynamic import is intentional: the extension validates launch-scoped identity at module load.
const { default: registerExtension } = await import("./longhouse-pi-helm");

const register = () => {
  const tools = new Map<string, RegisteredTool>();
  registerExtension({
    on: () => undefined,
    registerTool: (tool: RegisteredTool) => tools.set(tool.name, tool),
  } as never);
  return tools;
};

describe("Pi coordination tools", () => {
  it("registers the same contract tools as every other surface", () => {
    expect([...register().keys()].sort()).toEqual(
      ["inbox", "peers", "recall", "recall_context", "reply", "search_sessions", "send", "tail"],
    );
  });

  it("calls the Runtime Host as this session with its coordination authority", async () => {
    const previous = {
      url: process.env.LONGHOUSE_PI_HELM_URL,
      token: process.env.LONGHOUSE_COORDINATION_TOKEN,
    };
    const seen: Array<{ path: string; token: string | null; session: string | null }> = [];
    const server = Bun.serve({
      hostname: "127.0.0.1",
      port: 0,
      fetch(request) {
        seen.push({
          path: new URL(request.url).pathname,
          token: request.headers.get("X-Agents-Token"),
          session: request.headers.get("X-Longhouse-Session-Id"),
        });
        return Response.json({ sessions: [] });
      },
    });
    try {
      process.env.LONGHOUSE_PI_HELM_URL = server.url.origin;
      process.env.LONGHOUSE_COORDINATION_TOKEN = "pi-coordination-token";
      const response = await register()
        .get("peers")!
        .execute("peers-call", { repo: "/tmp/repo" }, new AbortController().signal, undefined, {});
      expect(response.isError).toBeUndefined();
      expect(JSON.parse(response.content[0].text)).toMatchObject({ total: 0, peers: [] });
      expect(seen).toEqual([
        { path: "/api/agents/sessions/wall", token: "pi-coordination-token", session: "pi-helm-test-session" },
      ]);
    } finally {
      process.env.LONGHOUSE_PI_HELM_URL = previous.url;
      if (previous.token === undefined) delete process.env.LONGHOUSE_COORDINATION_TOKEN;
      else process.env.LONGHOUSE_COORDINATION_TOKEN = previous.token;
      server.stop(true);
    }
  });

  it("refuses without authority and never lends it to a native subagent", async () => {
    delete process.env.LONGHOUSE_COORDINATION_TOKEN;
    const tools = register();
    const pending = await tools.get("tail")!.execute("tail-call", { session_id: "x" }, new AbortController().signal, undefined, {});
    expect(pending.isError).toBe(true);
    expect(pending.content[0].text).toContain("coordination authority is unavailable");
    const child = await tools
      .get("tail")!
      .execute("tail-call", { session_id: "x" }, new AbortController().signal, undefined, { agent: { kind: "sub" } });
    expect(child.isError).toBe(true);
    expect(child.content[0].text).toContain("Native subagents");
  });
});
