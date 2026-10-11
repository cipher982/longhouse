// Contract tests for Longhouse's Claude Code mod (claude-mod/hooks/register.js)
// against a fake mods API: what it asks the engine, and what it hands Claude.
import { describe, expect, it } from "bun:test";

type Hook = ($: unknown, e: Record<string, unknown>, next: (e: unknown) => Promise<unknown>) => Promise<unknown>;

let loads = 0;

/** A fresh copy of the module (its state is per copy, as in Claude Code) and a fake $. */
async function loadMod(lines: string[], options: { claimExit?: number; refuse?: string } = {}) {
  loads += 1;
  const { register } = await import(`./claude-mod/hooks/register.js?copy=${loads}`);
  const hooks = new Map<string, Hook>();
  register((event: string, hook: Hook) => hooks.set(event, hook));

  const runs: string[][] = [];
  const submits: Array<{ text: string; asUser: boolean }> = [];
  const aborts: string[] = [];
  let resolveTurn: () => void = () => undefined;
  let streamEnded: () => void = () => undefined;
  const streamDone = new Promise<void>((resolve) => {
    streamEnded = resolve;
  });
  let timers = 0;
  const $ = {
    env: {
      get: async (name: string) =>
        ({ LONGHOUSE_OWNER_INPUT_ENGINE: "/opt/longhouse-engine", LONGHOUSE_CHANNEL_SESSION_ID: "session-1" })[name],
    },
    clock: {
      now: async () => Date.now(),
      // Run the watcher and the refusal window; a watcher restart would only
      // replay the fake stream.
      after: (ms: number, fn: () => Promise<void>) => {
        timers += 1;
        if (timers === 1) void fn();
        else if (ms === 200) setTimeout(fn, 0);
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
      run: async (argv: string[]) => {
        runs.push(argv);
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
  return { fire, settle, runs, submits, aborts, acks, resolveTurn: () => resolveTurn() };
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
});
