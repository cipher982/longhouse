// Contract tests for Longhouse's Claude Code mod (claude-mod/hooks/register.js)
// against a fake mods API: what it asks the engine, and what it hands Claude.
import { describe, expect, it } from "bun:test";
import { readFileSync } from "node:fs";

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
    /** The watcher streams nothing until this settles. */
    gate?: Promise<void>;
    /** Slash commands this Claude Code knows; undefined: no $.command at all. */
    commands?: string[];
    /** $.command.run: what running a command does before it returns. */
    runCommand?: (command: { command: string; args: string }) => Promise<unknown>;
  } = {},
) {
  loads += 1;
  const { register } = await import(`./claude-mod/hooks/register.js?copy=${loads}`);
  const hooks = new Map<string, Hook>();
  register((event: string, matcherOrHook: unknown, maybeHook?: Hook) => {
    const hook = (maybeHook ?? matcherOrHook) as Hook;
    // A Claude Code before `turn.step` has no such event to subscribe to.
    if (options.noTurnStep && event === "turn.step") throw new Error("unknown event: turn.step");
    hooks.set(event, hook);
  });
  const fetches: FetchInit[] = [];

  const runs: string[][] = [];
  const submits: Array<{ text: string; asUser: boolean }> = [];
  const aborts: string[] = [];
  const commandRuns: Array<{ command: string; args: string }> = [];
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
          if (options.gate) await options.gate;
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
    command:
      options.commands === undefined
        ? undefined
        : {
            list: async () => options.commands!.map((name) => ({ name, description: "", source: "builtin" })),
            run: async (command: { command: string; args: string }) => {
              commandRuns.push(command);
              if (!options.commands!.includes(command.command)) throw new Error(`unknown command ${command.command}`);
              return options.runCommand ? options.runCommand(command) : { text: "" };
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
      .map((argv) => argv.slice(2).join(" ").replace(/--instance \S+/, "--instance <copy>"));
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
  return { $, fire, settle, runs, submits, aborts, commandRuns, appended, acks, step, fetches, sent, hooks, resolveTurn: () => resolveTurn() };
}

const line = (value: object) => JSON.stringify(value) + "\n";

const QUESTIONS = [
  {
    question: "How should cube-ws fetch the image?",
    header: "Image paste",
    options: [
      { label: "SSH on each paste", description: "ssh cinder per paste" },
      { label: "Longhouse tunnel", description: "a tunnel per session" },
    ],
    multiSelect: false,
  },
];

/** Opens AskUserQuestion through the mod, then lets the watcher stream `lines`. */
async function askWith(lines: string[], terminal: Promise<unknown> = new Promise(() => undefined)) {
  let open: () => void = () => undefined;
  const gate = new Promise<void>((resolve) => {
    open = resolve;
  });
  const mod = await loadMod(lines, { gate });
  await mod.fire("session.start");
  const passedToTerminal: unknown[] = [];
  const call = mod.hooks.get("tool.call")!(
    mod.$,
    { tool: "AskUserQuestion", tool_use_id: "toolu_q1", questions: QUESTIONS },
    async (e) => {
      passedToTerminal.push(e);
      return terminal;
    },
  );
  // Observed later by the test; a rejection is not unhandled meanwhile.
  call.catch(() => undefined);
  // The question is recorded before the dialog opens.
  await new Promise((resolve) => setTimeout(resolve, 10));
  open();
  await mod.settle();
  return { mod, call, passedToTerminal };
}

describe("Longhouse Claude mod", () => {
  it("runs the engine's watcher for this session", async () => {
    const mod = await loadMod([line({ kind: "ready" })]);
    await mod.fire("session.start");
    await mod.settle();
    expect(mod.runs[0].slice(0, 6)).toEqual([
      "/opt/longhouse-engine",
      "claude-owner-input",
      "watch",
      "--session",
      "session-1",
      "--instance",
    ]);
    // Each copy of the module names itself, so a reload drops what the last copy held.
    expect(mod.runs[0][6]).toMatch(/^[a-z0-9]+-[a-z0-9]+$/);
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

  it("runs an owner slash command as typed and reports a command that started no turn", async () => {
    const mod = await loadMod([line({ id: "cmd-1", kind: "send", text: " /rename my session", state: "pending" })], {
      commands: ["rename", "init"],
    });
    await mod.fire("session.start");
    await mod.settle();
    expect(mod.submits).toEqual([]);
    expect(mod.commandRuns).toEqual([{ command: "rename", args: "my session" }]);
    expect(mod.acks()).toEqual([
      "claim --id cmd-1 --session session-1",
      "ack --id cmd-1 --state command_ran --detail rename --session session-1",
    ]);
  });

  it("delivers a slash command that opens a model turn when that turn starts", async () => {
    let fire: (event: string, e?: Record<string, unknown>) => Promise<unknown> = async () => undefined;
    const mod = await loadMod([line({ id: "cmd-2", kind: "send", text: "/init", state: "pending" })], {
      commands: ["init"],
      runCommand: async () => {
        await fire("turn.start", { turnId: "turn-7", text: "Please analyze this codebase" });
        return {};
      },
    });
    fire = mod.fire;
    await mod.fire("session.start");
    await mod.settle();
    expect(mod.acks()).toEqual([
      "claim --id cmd-2 --session session-1",
      "ack --id cmd-2 --state delivered --turn-id turn-7 --session session-1",
    ]);
  });

  it("refuses a command this Claude Code does not have, and one with no command API", async () => {
    const unknown = await loadMod([line({ id: "cmd-3", kind: "send", text: "/nope", state: "pending" })], {
      commands: ["rename"],
    });
    await unknown.fire("session.start");
    await unknown.settle();
    expect(unknown.commandRuns).toEqual([]);
    expect(unknown.acks()[1]).toContain("--state failed --detail Claude Code has no /nope command");

    const old = await loadMod([line({ id: "cmd-4", kind: "send", text: "/rename x", state: "pending" })]);
    await old.fire("session.start");
    await old.settle();
    expect(old.submits).toEqual([]);
    expect(old.acks()[1]).toContain("--state failed --detail this Claude Code cannot run a command");
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

  it("emits throttled ClaudeModState on turn events and attaches snapshot to aborted Stop", async () => {
    const mod = await loadMod([], {
      agents: [{ id: "ag-2", type: "general-purpose", status: "waiting", description: "Waiting" }],
      sessionUsage: { context: { tokens: 90000, window: 1000000, percent: 9 }, cost: { usd: 3.5 } },
    });
    await mod.fire("session.start");
    await mod.settle();
    await mod.fire("turn.start", { turnId: "main-1", text: "first" });
    await mod.fire("turn.complete", { turnId: "main-1", reason: "end_turn" });
    await mod.fire("turn.start", { turnId: "main-2", text: "work" });
    await mod.fire("turn.complete", { turnId: "main-2", reason: "aborted" });

    const hookPayloads = mod.runs
      .filter((argv) => argv[1] === "claude-lifecycle-hook")
      .map((argv) => JSON.parse(argv[2].slice("stdin=".length)));
    expect(hookPayloads.some((p) => p.hook_event_name === "ClaudeModState")).toBe(true);
    // Debounce coalesces the second turn's snapshot right after the first.
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

describe("Longhouse Claude mod: source Claude Code accepts", () => {
  // Claude Code reads a mod's source before running it and refuses the whole module, owner input and live text
  // included, on a shape it cannot check (Claude Code 2.1.322: "$.agent is used as a value", "session.ready is not an
  // event"). Terminal Truth failed on main for a day on exactly this. These are the rules it enforced.
  const source = readFileSync(new URL("./claude-mod/hooks/register.js", import.meta.url), "utf8")
    .split("\n")
    .filter((text) => !text.trim().startsWith("//"))
    .join("\n");

  it("never reads a $ noun as a value; each is only called", () => {
    expect(source).not.toMatch(/typeof\s+\$\./);
    expect(source).not.toMatch(/\$\.[a-z]+\?\./);
    expect(source).not.toMatch(/[=(,]\s*\$\.[a-z]+\s*[,)]/);
  });

  it("subscribes with a literal event name from the events Claude Code has", () => {
    const events = [...source.matchAll(/\bon\(\s*([^,)]*)/g)].map((match) => match[1].trim());
    expect(events.length).toBeGreaterThan(0);
    for (const event of events) expect(event).toMatch(/^'(session\.start|tool\.call|turn\.(start|step|complete))'$/);
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

describe("Longhouse Claude mod: questions answered from the app", () => {
  it("answers Claude's question from the app when the app answers first", async () => {
    const { mod, call, passedToTerminal } = await askWith([
      line({
        id: "cmd-a",
        kind: "answer",
        state: "pending",
        answer: { tool_use_id: "toolu_q1", answers: { "How should cube-ws fetch the image?": "SSH on each paste" } },
      }),
    ]);
    expect(await call).toEqual({
      result: { questions: QUESTIONS, answers: { "How should cube-ws fetch the image?": "SSH on each paste" } },
    });
    // The terminal dialog was opened as asked, unchanged.
    expect(passedToTerminal).toEqual([{ tool: "AskUserQuestion", tool_use_id: "toolu_q1", questions: QUESTIONS }]);
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(mod.acks()).toEqual([
      `question-open --tool-use-id toolu_q1 --instance <copy> --session session-1 stdin=${JSON.stringify(QUESTIONS)}`,
      "claim --id cmd-a --session session-1",
      "ack --id cmd-a --state answered --session session-1",
      "question-close --tool-use-id toolu_q1 --session session-1",
    ]);
    // Longhouse hears the question closed even if Claude's PostToolUse never fires.
    const reported = mod.runs.find((argv) => argv[1] === "claude-lifecycle-hook");
    expect(JSON.parse(reported![2].slice("stdin=".length))).toEqual({
      hook_event_name: "PostToolUse",
      session_id: "provider-1",
      cwd: "/work",
      tool_name: "AskUserQuestion",
      tool_use_id: "toolu_q1",
      tool_input: { questions: QUESTIONS },
    });
  });

  it("hands Claude a free-text answer, and a declined question as a refusal", async () => {
    const free = await askWith([
      line({ id: "cmd-f", kind: "answer", state: "pending", answer: { tool_use_id: "toolu_q1", response: "Let's go with your recommendation" } }),
    ]);
    expect(await free.call).toEqual({
      result: { questions: QUESTIONS, answers: {}, response: "Let's go with your recommendation" },
    });

    const declined = await askWith([
      line({ id: "cmd-d", kind: "answer", state: "pending", answer: { tool_use_id: "toolu_q1", declined: true } }),
    ]);
    expect(await declined.call).toEqual({
      deny: "The user declined to answer this question from the Longhouse app.",
    });
    // The answer route records the decline; the mod must not record it as answered.
    expect(declined.mod.runs.some((argv) => argv[1] === "claude-lifecycle-hook")).toBe(false);
  });

  it("keeps the terminal's answer when the terminal answers first", async () => {
    const fromTerminal = { result: { questions: QUESTIONS, answers: { q: "Longhouse tunnel" } } };
    const { mod, call } = await askWith([], Promise.resolve(fromTerminal));
    expect(await call).toBe(fromTerminal);
    expect(mod.runs.some((argv) => argv[1] === "claude-lifecycle-hook")).toBe(false);
    expect(mod.acks().at(-1)).toBe("question-close --tool-use-id toolu_q1 --session session-1");
  });

  it("refuses an answer for a question that is no longer open", async () => {
    const mod = await loadMod([
      line({ id: "cmd-late", kind: "answer", state: "pending", answer: { tool_use_id: "toolu_gone", answers: {} } }),
    ]);
    await mod.fire("session.start");
    await mod.settle();
    expect(mod.acks()).toEqual([
      "claim --id cmd-late --session session-1",
      "ack --id cmd-late --state failed --detail the question was already answered in the terminal --session session-1",
    ]);
  });

  it("leaves a subagent's question to the terminal", async () => {
    const mod = await loadMod([]);
    await mod.fire("session.start");
    const result = await mod.hooks.get("tool.call")!(
      mod.$,
      { tool: "AskUserQuestion", tool_use_id: "toolu_s", questions: QUESTIONS, agentId: "sub-1" },
      async () => "terminal",
    );
    expect(result).toBe("terminal");
    expect(mod.acks()).toEqual([]);
  });

  it("refuses an app answer that arrives after the terminal answered", async () => {
    let terminalAnswered: (value: unknown) => void = () => undefined;
    const terminal = new Promise((resolve) => {
      terminalAnswered = resolve;
    });
    let open: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => {
      open = resolve;
    });
    const mod = await loadMod(
      [line({ id: "cmd-late", kind: "answer", state: "pending", answer: { tool_use_id: "toolu_q1", response: "too late" } })],
      { gate },
    );
    await mod.fire("session.start");
    const call = mod.hooks.get("tool.call")!(
      mod.$,
      { tool: "AskUserQuestion", tool_use_id: "toolu_q1", questions: QUESTIONS },
      async () => terminal,
    );
    await new Promise((resolve) => setTimeout(resolve, 10));
    const fromTerminal = { result: { questions: QUESTIONS, answers: { q: "Longhouse tunnel" } } };
    terminalAnswered(fromTerminal);
    // The answer reaches the mod while the terminal's is being handed back.
    open();
    await mod.settle();
    expect(await call).toBe(fromTerminal);
    expect(mod.acks()).toContain(
      "ack --id cmd-late --state failed --detail the question was already answered in the terminal --session session-1",
    );
    expect(mod.acks()).not.toContain("ack --id cmd-late --state answered --session session-1");
  });

  it("tells the sender when an earlier copy took the answer and reloaded", async () => {
    const mod = await loadMod([
      line({ id: "cmd-old", kind: "answer", state: "claimed", answer: { tool_use_id: "toolu_q1", response: "x" } }),
    ]);
    await mod.fire("session.start");
    await mod.settle();
    expect(mod.acks()).toEqual([
      "ack --id cmd-old --state failed --detail the Longhouse mod reloaded before answering --session session-1",
    ]);
  });

  it("passes a terminal failure through unchanged", async () => {
    const failed = Promise.reject(new Error("dialog failed"));
    failed.catch(() => undefined);
    const { call } = await askWith([], failed);
    await expect(call).rejects.toThrow("dialog failed");
  });
});
