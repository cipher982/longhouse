// Contract tests for Longhouse's Claude Code mod (claude-mod/hooks/register.js)
// against a fake mods API: what it asks the engine, and what it hands Claude.
import { describe, expect, it } from "bun:test";

type Hook = ($: unknown, e: Record<string, unknown>, next: (e: unknown) => Promise<unknown>) => Promise<unknown>;
type FetchInit = { url: string; method?: string; body: string; socketPath?: string };

let loads = 0;

/** A fresh copy of the module (its state is per copy, as in Claude Code) and a fake $. */
async function loadMod(
  lines: string[],
  options: {
    claimExit?: number;
    refuse?: string;
    liveSocket?: string;
    fetch?: (init: FetchInit) => Promise<{ ok: boolean; status: number }>;
    noTurnStep?: boolean;
    agents?: unknown[] | (() => Promise<unknown[]>);
    sessionUsage?: unknown | (() => Promise<unknown>);
  } = {},
) {
  loads += 1;
  const { register } = await import(`./claude-mod/hooks/register.js?copy=${loads}`);
  const hooks = new Map<string, Hook>();
  register((event: string, hook: Hook) => {
    // A Claude Code before `turn.step` has no such event to subscribe to.
    if (options.noTurnStep && event === "turn.step") throw new Error("unknown event: turn.step");
    hooks.set(event, hook);
  });
  const fetches: FetchInit[] = [];

  const runs: string[][] = [];
  const submits: Array<{ text: string; asUser: boolean }> = [];
  const aborts: string[] = [];
  const appended: unknown[] = [];
  let resolveTurn: () => void = () => undefined;
  let streamEnded: () => void = () => undefined;
  const streamDone = new Promise<void>((resolve) => {
    streamEnded = resolve;
  });
  let timers = 0;
  const $ = {
    env: {
      get: async (name: string) =>
        ({
          LONGHOUSE_OWNER_INPUT_ENGINE: "/opt/longhouse-engine",
          LONGHOUSE_CHANNEL_SESSION_ID: "session-1",
          LONGHOUSE_CLAUDE_LIVE_SOCKET: options.liveSocket,
        })[name],
    },
    http: {
      fetch: async (url: string, init: { method?: string; body: string; socketPath?: string }) => {
        const call = { url, ...init };
        fetches.push(call);
        return options.fetch ? options.fetch(call) : { ok: true, status: 200 };
      },
    },
    clock: {
      now: async () => Date.now(),
      // Run the watcher and the refusal window; a watcher restart would only
      // replay the fake stream.
      after: (ms: number, fn: () => Promise<void>) => {
        timers += 1;
        if (timers === 1) void fn();
        else if (ms === 200 || ms === 100) setTimeout(fn, 0);
        return { cancel: () => undefined };
      },
    },
    process: {
      spawn: ({ argv }: { argv: string[] }) => {
        runs.push(argv);
        return (async function* () {
          for (const text of lines) yield { stream: "stdout", text };
          streamEnded();
        })();
      },
      run: async (argv: string[], init?: { stdin?: string }) => {
        runs.push(init?.stdin ? [...argv, `stdin=${init.stdin}`] : argv);
        const exitCode = argv[2] === "claim" ? (options.claimExit ?? 0) : 0;
        return { exitCode, stdout: "", stderr: "" };
      },
    },
    prompt: {
      submit: (args: { text: string; asUser: boolean }) => {
        submits.push(args);
        if (options.refuse) return Promise.reject(new Error(options.refuse));
        return new Promise<void>((resolve) => {
          resolveTurn = resolve;
        });
      },
    },
    turn: {
      abort: async ({ turnId }: { turnId: string }) => {
        aborts.push(turnId);
      },
    },
    agent:
      options.agents === undefined
        ? undefined
        : {
            list: async () =>
              typeof options.agents === "function" ? options.agents() : options.agents,
          },
    session: {
      id: async () => "provider-1",
      cwd: async () => "/work",
      usage:
        options.sessionUsage === undefined
          ? undefined
          : async () =>
              typeof options.sessionUsage === "function"
                ? options.sessionUsage()
                : options.sessionUsage,
      append: async (row: unknown) => {
        appended.push(row);
        return { uuid: "row-1", message: row };
      },
    },
    ui: { log: () => undefined, status: () => undefined },
  };
  const fire = (event: string, e: Record<string, unknown> = {}) =>
    hooks.get(event)!($, e, async (passed) => passed);
  const settle = async () => {
    await streamDone;
    await new Promise((resolve) => setTimeout(resolve, 20));
  };
  const acks = () =>
    runs
      .filter((argv) => argv[1] === "claude-owner-input" && argv[2] !== "watch")
      .map((argv) => argv.slice(2).join(" "));
  // One model request: `chunks` come up through the mod's turn.step hook, which
  // must hand every one on unchanged. Returns what the hook yielded.
  const step = async (e: Record<string, unknown>, chunks: Array<Record<string, unknown>>) => {
    const hook = hooks.get("turn.step") as unknown as (
      $: unknown,
      e: Record<string, unknown>,
      next: (e: unknown) => AsyncGenerator<Record<string, unknown>>,
    ) => AsyncGenerator<Record<string, unknown>>;
    const next = (async function* () {
      for (const chunk of chunks) yield chunk;
      return { turnId: e.turnId };
    }) as (e: unknown) => AsyncGenerator<Record<string, unknown>>;
    const yielded: Array<Record<string, unknown>> = [];
    for await (const chunk of hook($, e, next)) yielded.push(chunk);
    return yielded;
  };
  const sent = () => fetches.flatMap((call) => JSON.parse(call.body) as Array<Record<string, unknown>>);
  return { fire, settle, runs, submits, aborts, appended, acks, step, fetches, sent, hooks, resolveTurn: () => resolveTurn() };
}

const line = (value: object) => JSON.stringify(value) + "\n";

describe("Longhouse Claude mod", () => {
  it("runs the engine's watcher for this session", async () => {
    const mod = await loadMod([line({ kind: "ready" })]);
    await mod.fire("session.start");
    await mod.settle();
    expect(mod.runs[0]).toEqual([
      "/opt/longhouse-engine",
      "claude-owner-input",
      "watch",
      "--session",
      "session-1",
    ]);
  });

  it("claims an owner message, submits it as the user's words, and records its turn", async () => {
    const message = line({ id: "cmd-1", kind: "send", text: "create proof.txt", state: "pending" });
    // A line split across pieces, as a pipe may deliver it.
    const mod = await loadMod([line({ kind: "ready" }), message.slice(0, 10), message.slice(10)]);
    await mod.fire("session.start");
    await mod.settle();
    expect(mod.submits).toEqual([{ text: "create proof.txt", asUser: true }]);
    await mod.fire("turn.start", { turnId: "turn-7", text: "create proof.txt" });
    mod.resolveTurn();
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(mod.acks()).toEqual([
      "claim --id cmd-1 --session session-1",
      "ack --id cmd-1 --state submitted --session session-1",
      "ack --id cmd-1 --state delivered --turn-id turn-7 --session session-1",
    ]);
  });

  it("reports a submit Claude Code refused as failed, never as submitted", async () => {
    const mod = await loadMod([line({ id: "cmd-1", kind: "send", text: "hello", state: "pending" })], {
      refuse: "prompt.submit: the turn is held",
    });
    await mod.fire("session.start");
    await mod.settle();
    expect(mod.acks()).toEqual([
      "claim --id cmd-1 --session session-1",
      "ack --id cmd-1 --state failed --detail Error: prompt.submit: the turn is held --session session-1",
    ]);
  });

  it("submits nothing the engine did not let it claim", async () => {
    const mod = await loadMod([line({ id: "cmd-1", kind: "send", text: "hello", state: "pending" })], {
      claimExit: 3,
    });
    await mod.fire("session.start");
    await mod.settle();
    expect(mod.submits).toEqual([]);
    expect(mod.acks()).toEqual(["claim --id cmd-1 --session session-1"]);
  });

  it("never resubmits what an earlier copy submitted, and records its turn when it starts", async () => {
    const mod = await loadMod([line({ id: "old", kind: "send", text: "earlier", state: "claimed" })]);
    await mod.fire("session.start");
    await mod.settle();
    expect(mod.submits).toEqual([]);
    await mod.fire("turn.start", { turnId: "turn-2", text: "earlier" });
    await mod.settle();
    expect(mod.acks()).toEqual(["ack --id old --state delivered --turn-id turn-2 --session session-1"]);
  });

  it("refuses text Claude Code would run as a command", async () => {
    const mod = await loadMod([line({ id: "cmd-1", kind: "send", text: " /compact", state: "pending" })]);
    await mod.fire("session.start");
    await mod.settle();
    expect(mod.submits).toEqual([]);
    expect(mod.acks()[1]).toContain("ack --id cmd-1 --state failed");
  });

  it("ends the running turn on abort and says when there was none", async () => {
    const mod = await loadMod([
      line({ id: "a-1", kind: "abort", state: "pending" }),
    ]);
    await mod.fire("turn.start", { turnId: "turn-3", text: "long job" });
    await mod.fire("session.start");
    await mod.settle();
    expect(mod.aborts).toEqual(["turn-3"]);
    expect(mod.acks()).toEqual([
      "claim --id a-1 --session session-1",
      "ack --id a-1 --state aborted --turn-id turn-3 --session session-1",
    ]);

    const idle = await loadMod([line({ id: "a-2", kind: "abort", state: "pending" })]);
    await idle.fire("session.start");
    await idle.settle();
    expect(idle.aborts).toEqual([]);
    expect(idle.acks()).toEqual([
      "claim --id a-2 --session session-1",
      "ack --id a-2 --state no_turn --session session-1",
    ]);
  });

  it("ends an owner-aborted turn as Esc does and reports it to Longhouse", async () => {
    const mod = await loadMod([line({ id: "a-1", kind: "abort", state: "pending" })]);
    await mod.fire("turn.start", { turnId: "turn-3", text: "long job" });
    await mod.fire("session.start");
    await mod.settle();
    await mod.fire("turn.complete", { turnId: "turn-3", reason: "aborted" });
    expect(mod.appended).toEqual([
      { message: { type: "user", content: [{ type: "text", text: "[Request interrupted by user]" }] } },
    ]);
    const stop = mod.runs.find((argv) => argv[1] === "claude-lifecycle-hook");
    expect(stop?.[0]).toBe("/opt/longhouse-engine");
    expect(JSON.parse(stop![2].slice("stdin=".length))).toEqual({
      hook_event_name: "Stop",
      session_id: "provider-1",
      cwd: "/work",
      stop_hook_active: false,
      aborted: true,
    });
  });

  it("reports a turn the user ended with Esc, without a second interrupted row", async () => {
    const mod = await loadMod([]);
    await mod.fire("session.start");
    await mod.settle();
    await mod.fire("turn.start", { turnId: "turn-8", text: "x" });
    await mod.fire("turn.complete", { turnId: "turn-8", reason: "aborted" });
    expect(mod.appended).toEqual([]);
    expect(mod.runs.some((argv) => argv[1] === "claude-lifecycle-hook")).toBe(true);
    // A second completion of the same turn reports nothing more.
    await mod.fire("turn.complete", { turnId: "turn-8", reason: "aborted" });
    expect(mod.runs.filter((argv) => argv[1] === "claude-lifecycle-hook").length).toBe(1);

    const answered = await loadMod([]);
    await answered.fire("session.start");
    await answered.settle();
    await answered.fire("turn.start", { turnId: "turn-9", text: "y" });
    await answered.fire("turn.complete", { turnId: "turn-9", reason: "answer" });
    expect(answered.runs.some((argv) => argv[1] === "claude-lifecycle-hook")).toBe(false);
  });

  it("forgets the turn when it completes, but not on a subagent's", async () => {
    // A subagent's completion leaves the main loop's turn running.
    const abortLines = await loadMod([line({ id: "a-3", kind: "abort", state: "pending" })]);
    await abortLines.fire("turn.start", { turnId: "turn-5", text: "y" });
    await abortLines.fire("turn.complete", { turnId: "turn-5", agentId: "sub-1" });
    await abortLines.fire("session.start");
    await abortLines.settle();
    expect(abortLines.aborts).toEqual(["turn-5"]);

    const ended = await loadMod([line({ id: "a-4", kind: "abort", state: "pending" })]);
    await ended.fire("turn.start", { turnId: "turn-6", text: "z" });
    await ended.fire("turn.complete", { turnId: "turn-6" });
    await ended.fire("session.start");
    await ended.settle();
    expect(ended.aborts).toEqual([]);
  });

  it("ignores child turn.start so main turnId stays intact and forwards agent/usage snapshots", async () => {
    const abortMod = await loadMod([line({ id: "a-child", kind: "abort", state: "pending" })]);
    await abortMod.fire("turn.start", { turnId: "main-turn", text: "root" });
    await abortMod.fire("turn.start", { turnId: "child-turn", agentId: "ag-1", text: "sub" });
    await abortMod.fire("session.start");
    await abortMod.settle();
    expect(abortMod.aborts).toEqual(["main-turn"]);

    const mod = await loadMod([], {
      agents: [{ id: "ag-1", type: "Explore", status: "running", description: "Check tree" }],
      sessionUsage: {
        startedAt: 1000,
        context: { tokens: 42000, window: 1000000, percent: 4 },
        cost: { usd: 1.25 },
        rateLimits: [{ kind: "five_hour", percentUsed: 12, resetsAt: "2026-11-15T06:00:00Z" }],
      },
    });
    await mod.fire("session.start");
    await mod.settle();
    await mod.fire("turn.start", { turnId: "child-turn", agentId: "ag-1", text: "sub" });
    await mod.settle();

    const snapshotCalls = mod.runs
      .filter((argv) => argv[1] === "claude-lifecycle-hook")
      .map((argv) => JSON.parse(argv[2].slice("stdin=".length)));
    expect(snapshotCalls.length).toBeGreaterThanOrEqual(1);
    expect(snapshotCalls[0]).toEqual({
      hook_event_name: "ClaudeModState",
      trigger: "turn.start",
      session_id: "provider-1",
      cwd: "/work",
      agents: [{ id: "ag-1", type: "Explore", status: "running", description: "Check tree" }],
      session_usage: {
        startedAt: 1000,
        context: { tokens: 42000, window: 1000000, percent: 4 },
        cost: { usd: 1.25 },
        rateLimits: [{ kind: "five_hour", percentUsed: 12, resetsAt: "2026-11-15T06:00:00Z" }],
      },
    });
  });

  it("emits throttled ClaudeModState on session.ready and session.measure and attaches snapshot to aborted Stop", async () => {
    const mod = await loadMod([], {
      agents: [{ id: "ag-2", type: "general-purpose", status: "waiting", description: "Waiting" }],
      sessionUsage: { context: { tokens: 90000, window: 1000000, percent: 9 }, cost: { usd: 3.5 } },
    });
    await mod.fire("session.start");
    await mod.settle();
    await mod.fire("session.ready");
    await mod.fire("session.measure");
    await mod.fire("turn.start", { turnId: "main-2", text: "work" });
    await mod.fire("turn.complete", { turnId: "main-2", reason: "aborted" });

    const hookPayloads = mod.runs
      .filter((argv) => argv[1] === "claude-lifecycle-hook")
      .map((argv) => JSON.parse(argv[2].slice("stdin=".length)));
    expect(hookPayloads.some((p) => p.hook_event_name === "ClaudeModState")).toBe(true);
    // Debounce coalesces immediate session.measure right after session.ready.
    expect(hookPayloads.filter((p) => p.hook_event_name === "ClaudeModState").length).toBe(1);
    const stopPayload = hookPayloads.find((p) => p.hook_event_name === "Stop");
    expect(stopPayload).toMatchObject({
      hook_event_name: "Stop",
      aborted: true,
      agents: [{ id: "ag-2", type: "general-purpose", status: "waiting", description: "Waiting" }],
      session_usage: { context: { tokens: 90000, window: 1000000, percent: 9 }, cost: { usd: 3.5 } },
    });
  });
});

describe("Longhouse Claude mod: live reply text", () => {
  const SOCKET = "/tmp/longhouse-claude-live.sock";
  const settle = () => new Promise((resolve) => setTimeout(resolve, 20));
  const text = (index: number, value: string) => ({ kind: "text", index, text: value });

  async function started(options: Parameters<typeof loadMod>[1] = {}) {
    const mod = await loadMod([], { liveSocket: SOCKET, ...options });
    await mod.fire("session.start");
    await mod.settle();
    return mod;
  }

  it("forwards reply text, thinking, tool starts and tool arguments, labelled, and hands every chunk on unchanged", async () => {
    const mod = await started();
    const chunks = [
      { kind: "thinking", index: 0, text: "secret " },
      { kind: "thinking", index: 0, text: "reasoning" },
      text(1, "Hel"),
      text(1, "lo"),
      { kind: "tool", index: 2, id: "toolu_1", name: "Bash" },
      { kind: "input", index: 2, json: '{"command":"ca' },
      { kind: "input", index: 2, json: 't /etc/hosts"}' },
      { kind: "stop", stopReason: "end_turn", usage: null },
    ];
    const yielded = await mod.step({ turnId: "t1", index: 0, model: "m", messageCount: 1 }, chunks);
    await settle();
    expect(yielded).toEqual(chunks);
    yielded.forEach((chunk, i) => expect(chunk).toBe(chunks[i]));
    expect(mod.fetches.every((call) => call.socketPath === SOCKET && call.method === "POST")).toBe(true);
    const sent = mod.sent();
    // Reply text carries no kind, so an older engine reads it as it always did.
    expect(sent.filter((entry) => entry.kind === undefined).map((entry) => entry.text).join("")).toBe("Hello");
    expect(sent.filter((entry) => entry.kind === "thinking").map((entry) => entry.text).join("")).toBe(
      "secret reasoning",
    );
    expect(sent.filter((entry) => entry.kind === "tool")).toEqual([
      { turn: "t1", step: 0, kind: "tool", index: 2, id: "toolu_1", name: "Bash" },
    ]);
    // The argument JSON pieces are joined in order, under their block.
    expect(sent.filter((entry) => entry.kind === "input")).toEqual([
      { turn: "t1", step: 0, kind: "input", index: 2, json: '{"command":"cat /etc/hosts"}' },
    ]);
    expect(sent.every((entry) => entry.turn === "t1" && entry.step === 0)).toBe(true);
    // Thinking text never rides in an entry without its kind.
    expect(sent.some((entry) => entry.kind === undefined && String(entry.text).includes("secret"))).toBe(false);
    const wire = mod.fetches.map((call) => call.body).join("");
    for (const dropped of ["end_turn"]) {
      expect(wire).not.toContain(dropped);
    }
  });

  it("forwards no thinking or tool start for a subagent's steps", async () => {
    const mod = await started();
    const chunks = [{ kind: "thinking", index: 0, text: "child thought" }, { kind: "tool", index: 1, id: "c1", name: "Read" }];
    await mod.step({ turnId: "t9", index: 0, model: "m", messageCount: 1, agentId: "sub-1" }, chunks);
    await settle();
    expect(mod.fetches).toEqual([]);
  });

  it("coalesces a fast stream: the first piece goes at once, the rest wait for the spacing", async () => {
    let release: () => void = () => undefined;
    let calls = 0;
    const mod = await started({
      fetch: () => {
        calls += 1;
        if (calls > 1) return Promise.resolve({ ok: true, status: 200 });
        return new Promise((resolve) => {
          release = () => resolve({ ok: true, status: 200 });
        });
      },
    });
    const pieces = Array.from({ length: 50 }, (_, i) => text(0, `w${i} `));
    await mod.step({ turnId: "t1", index: 0, model: "m", messageCount: 1 }, pieces);
    // The first post is still in flight: nothing else has gone out.
    expect(mod.fetches.length).toBe(1);
    release();
    await settle();
    expect(mod.fetches.length).toBe(2);
    expect(mod.sent().map((entry) => entry.text).join("")).toBe(pieces.map((piece) => piece.text).join(""));
    // 50 pieces, two posts, one entry per post.
    expect(mod.sent().length).toBe(2);
  });

  it("names the step of each piece and says when a turn ends", async () => {
    const mod = await started();
    await mod.step({ turnId: "t1", index: 0, model: "m", messageCount: 1 }, [text(0, "before the tool")]);
    await settle();
    await mod.step({ turnId: "t1", index: 1, model: "m", messageCount: 3 }, [text(0, "after")]);
    await settle();
    await mod.fire("turn.complete", { turnId: "t1", reason: "answer" });
    await settle();
    expect(mod.sent()).toEqual([
      { turn: "t1", step: 0, text: "before the tool" },
      { turn: "t1", step: 1, text: "after" },
      { turn: "t1", done: true },
    ]);
  });

  it("forwards nothing for a subagent's steps, and sends no done for its turn", async () => {
    const mod = await started();
    const chunks = [text(0, "sub text")];
    const yielded = await mod.step({ turnId: "t9", index: 0, model: "m", messageCount: 1, agentId: "sub-1" }, chunks);
    await mod.fire("turn.complete", { turnId: "t9", agentId: "sub-1" });
    await settle();
    expect(yielded).toEqual(chunks);
    expect(mod.fetches).toEqual([]);
  });

  it("stays silent without the launcher's socket, and still passes chunks on", async () => {
    const mod = await loadMod([]);
    await mod.fire("session.start");
    await mod.settle();
    const chunks = [text(0, "hi")];
    expect(await mod.step({ turnId: "t1", index: 0, model: "m", messageCount: 1 }, chunks)).toEqual(chunks);
    await mod.fire("turn.complete", { turnId: "t1" });
    await settle();
    expect(mod.fetches).toEqual([]);
  });

  it("gives up after repeated refusals without throwing into Claude", async () => {
    const mod = await started({ fetch: () => Promise.reject(new Error("connect ENOENT")) });
    for (let i = 0; i < 6; i++) {
      const chunks = [text(0, `piece ${i}`)];
      expect(await mod.step({ turnId: "t1", index: i, model: "m", messageCount: 1 }, chunks)).toEqual(chunks);
      await settle();
    }
    expect(mod.fetches.length).toBe(3);
    await mod.fire("turn.complete", { turnId: "t1" });
    await settle();
    expect(mod.fetches.length).toBe(3);
  });

  it("loads and does nothing for live text on a Claude Code without turn.step", async () => {
    const mod = await started({ noTurnStep: true });
    expect(mod.hooks.has("turn.step")).toBe(false);
    // Owner input works as it did.
    expect(mod.hooks.has("session.start") && mod.hooks.has("turn.start") && mod.hooks.has("turn.complete")).toBe(true);
    await mod.fire("turn.complete", { turnId: "t1", reason: "answer" });
    expect(mod.fetches).toEqual([]);
  });

  it("does not hold a chunk back while it posts", async () => {
    // The post never completes; the stream must still run to its end.
    const mod = await started({ fetch: () => new Promise(() => undefined) });
    const pieces = [text(0, "a"), text(0, "b"), text(0, "c")];
    const yielded = await mod.step({ turnId: "t1", index: 0, model: "m", messageCount: 1 }, pieces);
    expect(yielded).toEqual(pieces);
  });
});
