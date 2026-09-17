import { connect, type Socket } from "node:net";

type Frame = Record<string, unknown>;

const socketPath = process.env.LONGHOUSE_OMP_HELM_CHANNEL_PATH;
const authToken = process.env.LONGHOUSE_OMP_HELM_CHANNEL_TOKEN;
const launchSessionId = process.env.LONGHOUSE_MANAGED_SESSION_ID;
const initialPrompt = process.env.LONGHOUSE_OMP_HELM_INITIAL_PROMPT ?? "";
const initialPromptDeliveredAtLaunch =
  process.env.LONGHOUSE_OMP_HELM_INITIAL_PROMPT_DELIVERED === "1";
const MAX_FRAME_BYTES = 512 * 1024;
const MAX_METADATA_STRING_LENGTH = 256;
const MALFORMED_BOOLEAN_MARKER = "__omp_malformed_boolean__";
const MAX_LIVE_TEXT_DELTA_LENGTH = 4096;


if (!socketPath || !authToken || !launchSessionId) {
  throw new Error("Longhouse OMP Helm extension is missing launch-scoped channel identity");
}

export function ompProviderIsIdle(
  lastAgentEndTerminal: boolean | undefined,
  contextIdle: boolean,
): boolean {
  // A continuation must stay live through a transient idle sample. A terminal
  // result is re-sampled from the provider context so a new turn that begins
  // while the channel is disconnected cannot remain falsely idle.
  return lastAgentEndTerminal === false ? false : contextIdle;
}

export function agentEndIsTerminal(event: Record<string, unknown>): boolean {
  // OMP emits `willContinue: t?.willContinue` on every extension agent_end, so
  // a final turn carries the key with the value `undefined`. Upstream reads
  // that as terminal (`isTerminal: !decision?.willContinue`); an undefined
  // field is absent, not malformed. Treating it as malformed made every OMP
  // turn a continuation, so no Helm turn could ever settle. `null` and other
  // non-boolean values stay malformed and non-terminal.
  for (const key of ["isTerminal", "willContinue"]) {
    if (event[key] !== undefined && typeof event[key] !== "boolean") return false;
  }
  if (typeof event.isTerminal === "boolean") return event.isTerminal;
  if (typeof event.willContinue === "boolean") return !event.willContinue;
  return true;
}


export default function (pi: any) {
  let socket: Socket | undefined;
  let buffer = "";
  let generation = 0;
  let ready = false;
  let shuttingDown = false;
  let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
  let connectionPromise: Promise<void> | undefined;
  let connectionId = "";
  let reconnectAttempts = 0;
  let keepaliveTimer: ReturnType<typeof setInterval> | undefined;
  /// Far shorter than the launcher's 90s deadline, so silence is evidence.
  const KEEPALIVE_INTERVAL_MS = 20000;
  let leaseGeneration = "";
  let commandChain = Promise.resolve();
  let initialPromptDelivered = initialPromptDeliveredAtLaunch;
  let initialPromptAttempts = 0;
  /// The launcher grants only once the session is ready, and a session becomes
  /// ready asynchronously. A single request therefore loses the prompt for good
  /// when it lands first: the TUI comes up looking normal with no prompt in it.
  /// Ask again until granted, bounded so a launcher that never grants cannot
  /// spin forever. The retry needs no timer handle: it stops on delivery.
  const INITIAL_PROMPT_RETRY_MS = 250;
  const INITIAL_PROMPT_MAX_ATTEMPTS = 240;
  let lastAgentEndTerminal: boolean | undefined;
  let turnGeneration = 0;
  const generationWaiters: Array<() => void> = [];

  const waitForGenerationChange = (previous: string) =>
    new Promise<boolean>((resolve) => {
      if (leaseGeneration && leaseGeneration !== previous) {
        resolve(true);
        return;
      }
      const timer = setTimeout(() => {
        const index = generationWaiters.indexOf(done);
        if (index >= 0) generationWaiters.splice(index, 1);
        resolve(false);
      }, 2000);
      const done = () => {
        clearTimeout(timer);
        resolve(leaseGeneration !== previous);
      };
      generationWaiters.push(done);
    });
  const deferredCommands: Frame[] = [];

  const session = (ctx: any) => ({
    session_id: launchSessionId,
    native_session_id: ctx.sessionManager.getSessionId(),
    session_file: ctx.sessionManager.getSessionFile(),
    connection_id: connectionId,
    lease_generation: leaseGeneration,
    turn_generation: turnGeneration,
    agent_end_terminal: lastAgentEndTerminal,
    auth_token: authToken,
  });


  const write = (frame: Frame, expectedGeneration = generation) => {
    if (!socket || !ready || expectedGeneration !== generation || socket.destroyed) return false;
    const bytes = Buffer.from(`${JSON.stringify(frame)}\n`, "utf8");
    if (bytes.byteLength > MAX_FRAME_BYTES) return false;
    socket.write(bytes);
    return true;
  };

  const close = () => {
    generation += 1;
    ready = false;
    buffer = "";
    connectionPromise = undefined;
    commandChain = Promise.resolve();
    const old = socket;
    socket = undefined;
    old?.end();
    old?.removeAllListeners();
  };

  const compactLifecycleEvent = (kind: string, event: Frame): Frame => {
    const compact: Frame = {
      type: typeof event.type === "string" ? event.type.slice(0, MAX_METADATA_STRING_LENGTH) : kind,
    };
    for (const key of ["reason", "request_id", "turn_id", "run_id", "title", "toolName", "toolCallId"]) {
      if (typeof event[key] === "string") {
        compact[key] = event[key].slice(0, MAX_METADATA_STRING_LENGTH);
      }
    }
    for (const key of ["isTerminal", "willContinue"]) {
      if (key in event) {
        const value = event[key];
        if (typeof value === "boolean") {
          compact[key] = value;
        } else if (value !== null && value !== undefined) {
          compact[key] = MALFORMED_BOOLEAN_MARKER;
        }
      }
    }
    if (kind === "agent_end") {
      // Preserve the exact terminal decision used by keepalive polling.
      // OMP may expose null/undefined lifecycle fields that compaction drops.
      compact.isTerminal = agentEndIsTerminal(event);
    }
    if (kind === "message_update" && event.assistantMessageEvent && typeof event.assistantMessageEvent === "object") {
      const update = event.assistantMessageEvent as Frame;
      if (typeof update.type === "string") {
        compact.message_event_type = update.type.slice(0, MAX_METADATA_STRING_LENGTH);
        if (update.type === "text_delta" && typeof update.delta === "string") {
          compact.delta = update.delta.slice(0, MAX_LIVE_TEXT_DELTA_LENGTH);
        }
      }
    }

    for (const key of ["success", "isError", "provider_idle"]) {
      if (typeof event[key] === "boolean") compact[key] = event[key];
    }
    return compact;
  };

  // OMP's `agent_end` is not always followed by an idle provider: a
  // continuation/retry can begin immediately. Sample the live context on
  // every keepalive, but preserve an explicit terminal OMP event because its
  // event shape is the provider-specific settlement signal.
  const providerIsIdle = (ctx: any) =>
    ompProviderIsIdle(lastAgentEndTerminal, Boolean(ctx.isIdle()));


  const sendEvent = (kind: string, event: Frame, ctx: any) =>
    write({ kind, event: compactLifecycleEvent(kind, event), ...session(ctx) });

  /// Ask for the initial prompt until the launcher grants it.
  ///
  /// The grant requires a ready session, and a refusal is not informative: it
  /// means "not yet". Without this the prompt is dropped silently for the life
  /// of the session, because the launcher only answers the request it is sent.
  ///
  /// `ctx` stays `unknown` here: this file's provider context is untyped, and a
  /// new signature is no place to widen that.
  const requestInitialPrompt = (ctx: unknown) => {
    if (initialPromptDelivered || !initialPrompt?.trim()) return;
    if (initialPromptAttempts >= INITIAL_PROMPT_MAX_ATTEMPTS) return;
    initialPromptAttempts += 1;
    sendEvent("initial_prompt_request", { type: "initial_prompt_request" }, ctx);
    setTimeout(() => requestInitialPrompt(ctx), INITIAL_PROMPT_RETRY_MS);
  };

  const handleFrame = (frame: Frame, ctx: any, ownGeneration: number) => {
    if (ownGeneration !== generation) return;
    if (frame.kind === "extension_ready") {
      if (frame.ok !== true) throw new Error(String((frame.error as Frame | undefined)?.message ?? "OMP Helm handshake rejected"));
      connectionId = typeof frame.connection_id === "string" ? frame.connection_id : "";
      leaseGeneration = typeof frame.lease_generation === "string" ? frame.lease_generation : "";
      ready = Boolean(connectionId && leaseGeneration);
      return;
    }
    if (frame.kind === "extension_generation") {
      connectionId = typeof frame.connection_id === "string" ? frame.connection_id : connectionId;
      leaseGeneration = typeof frame.lease_generation === "string" ? frame.lease_generation : leaseGeneration;
      ready = Boolean(connectionId && leaseGeneration);
      const waiters = generationWaiters.splice(0);
      for (const done of waiters) done();
      return;
    }
    if (frame.kind === "initial_prompt_grant") {
      if (frame.granted === true && initialPrompt?.trim() && !initialPromptDelivered) {
        initialPromptDelivered = true;
        void Promise.resolve(pi.sendUserMessage(initialPrompt)).catch((error: unknown) => {
          // Swallowing this hid a session that came up looking healthy with no
          // prompt in it at all. The provider refuses the send for real reasons
          // ("No model selected" on a fresh profile), and nothing downstream
          // could tell that apart from a delivered prompt.
          sendEvent(
            "initial_prompt_failed",
            {
              type: "initial_prompt_failed",
              message: error instanceof Error ? error.message : String(error),
            },
            ctx,
          );
        });
      }
      // A refusal only means "not ready yet". The retry chain already running
      // from session_start will ask again; delivery clears it.
      return;
    }
    if (["send", "steer", "abort", "terminate"].includes(String(frame.kind))) {
      if (!ctx) {
        if (deferredCommands.length < 32) deferredCommands.push(frame);
        return;
      }
      const commandGeneration = ownGeneration;
      commandChain = commandChain
        .then(() => handleCommand(frame, ctx, commandGeneration))
        .catch(() => undefined);
    }
  };

  const scheduleReconnect = (ctx: any) => {
    if (shuttingDown || reconnectTimer) return;
    reconnectTimer = setTimeout(async () => {
      reconnectTimer = undefined;
      reconnectAttempts += 1;
      try {
        await connectChannel(ctx);
        // A reconnect can cross a provider turn boundary while the channel is
        // down. The turn generation and terminal decision in the snapshot let
        // the launcher distinguish a missed new turn from old drain frames.
        const providerIdle = ompProviderIsIdle(lastAgentEndTerminal, Boolean(ctx.isIdle()));
        reconnectAttempts = 0;
        sendEvent(
          "session_reconnect",
          { type: "session_reconnect", provider_idle: providerIdle },
          ctx,
        );
        scheduleReconnect(ctx);
      } catch {
        scheduleReconnect(ctx);
      }
    }, Math.min(1000, 100 * 2 ** Math.max(0, reconnectAttempts - 1)));
  };

  const connectChannel = (ctx: any): Promise<void> => {
    if (connectionPromise) return connectionPromise;
    const ownGeneration = ++generation;
    connectionPromise = new Promise<void>((resolve, reject) => {
      const candidate = connect(socketPath, () => {
        // Advertise the keepalive so the launcher may hold this channel to a
        // read deadline. Without the declaration a silent channel is
        // indistinguishable from an idle provider, and the launcher keeps the
        // unbounded read it has always used.
        candidate.write(
          `${JSON.stringify({ kind: "extension_hello", keepalive: true, ...session(ctx) })}\n`,
        );
        if (keepaliveTimer) clearInterval(keepaliveTimer);
        keepaliveTimer = setInterval(() => {
          if (shuttingDown || socket !== candidate) return;
          try {
            candidate.write(
              `${JSON.stringify({
                kind: "extension_keepalive",
                provider_idle: providerIsIdle(ctx),
                ...session(ctx),
              })}\n`,
            );
          } catch {
            // A failed write is the channel telling us it is gone; the read
            // deadline on the launcher side is what turns that into a
            // reconnect.
          }
        }, KEEPALIVE_INTERVAL_MS);
      });
      socket = candidate;
      const fail = (error: Error) => {
        if (ownGeneration !== generation) return;
        ready = false;
        connectionPromise = undefined;
        reject(error);
      };
      candidate.on("data", (chunk: Buffer) => {
        if (ownGeneration !== generation) return;
        buffer += chunk.toString("utf8");
        if (Buffer.byteLength(buffer, "utf8") > MAX_FRAME_BYTES) {
          fail(new Error("OMP Helm channel frame exceeds limit"));
          candidate.destroy();
          return;
        }
        let newline = buffer.indexOf("\n");
        while (newline >= 0) {
          const raw = buffer.slice(0, newline);
          buffer = buffer.slice(newline + 1);
          newline = buffer.indexOf("\n");
          if (!raw.trim()) continue;
          try {
            handleFrame(JSON.parse(raw) as Frame, ctx, ownGeneration);
            if (ready) resolve();
          } catch (error) {
            fail(error instanceof Error ? error : new Error(String(error)));
            candidate.destroy();
            return;
          }
        }
      });
      candidate.once("error", fail);
      candidate.once("close", () => {
        if (ownGeneration !== generation) return;
        ready = false;
        socket = undefined;
        connectionPromise = undefined;
        scheduleReconnect(ctx);
      });
    });
    return connectionPromise;
  };

  const handleCommand = async (command: Frame, ctx: any, ownGeneration: number) => {
    if (!ctx) return;
    const current = session(ctx);
    const authorityFields = ["auth_token", "session_id", "native_session_id", "session_file", "connection_id", "lease_generation"];
    const authorityMatches = authorityFields.every((field) => command[field] === current[field]);
    const kind = String(command.kind);
    const text = typeof command.text === "string" ? command.text : "";
    let reply: Frame = { kind: "command_result", request_id: command.request_id, ok: false, ...current };
    try {
      if (!authorityMatches) throw new Error("OMP Helm command authority is stale");
      if (["send", "steer"].includes(kind) && !text.trim()) throw new Error("OMP Helm input text must not be empty");
      if (kind === "send") {
        if (providerIsIdle(ctx)) await Promise.resolve(pi.sendUserMessage(text));
        else await Promise.resolve(pi.sendUserMessage(text, { deliverAs: "followUp" }));
      } else if (kind === "steer") {
        if (providerIsIdle(ctx)) throw new Error("OMP provider has no active turn to steer");
        await Promise.resolve(pi.sendUserMessage(text, { deliverAs: "steer" }));
      } else if (kind === "abort") {
        await Promise.resolve(ctx.abort());
      } else if (kind === "terminate") {
        await Promise.resolve(ctx.shutdown());
      } else {
        throw new Error(`unknown OMP Helm command: ${kind}`);
      }
      reply = { ...reply, ok: true, status: providerIsIdle(ctx) ? "idle" : "active" };
    } catch (error) {
      reply.error = {
        code: !authorityMatches
          ? "stale_channel"
          : kind === "steer" && providerIsIdle(ctx)
            ? "turn_ended"
            : "command_failed",
        message: error instanceof Error ? error.message : String(error),
      };
    }
    write(reply, ownGeneration);
  };

  const lifecycle = (name: string, event: Frame, ctx: any) => {
    return sendEvent(name, event, ctx);
  };

  const waitForReplacement = async (name: string, event: Frame, ctx: any) => {
    const previous = leaseGeneration;
    if (!lifecycle(name, event, ctx)) return false;
    const changed = await waitForGenerationChange(previous);
    if (!changed) {
      write({
        kind: "session_transition_cancelled",
        transition: name,
        ...session(ctx),
      });
    }
    return changed;
  };

  pi.on("session_start", async (event: Frame, ctx: any) => {
    shuttingDown = false;
    lastAgentEndTerminal = undefined;
    if (socket) close();
    await connectChannel(ctx);
    lifecycle("session_start", event, ctx);
    if (event.type === "session_start" && initialPrompt?.trim()) {
      requestInitialPrompt(ctx);
    }
    while (deferredCommands.length) {
      const command = deferredCommands.shift()!;
      commandChain = commandChain.then(() => handleCommand(command, ctx, generation));
    }
  });
  pi.on("session_before_switch", async (event: Frame, ctx: any) => {
    const completed = await waitForReplacement("session_before_switch", event, ctx);
    return completed ? undefined : { cancel: true };
  });
  pi.on("session_switch", async (event: Frame, ctx: any) => {
    lastAgentEndTerminal = undefined;
    lifecycle("session_switch", event, ctx);
  });
  pi.on("session_before_branch", async (event: Frame, ctx: any) => {
    const completed = await waitForReplacement("session_before_branch", event, ctx);
    return completed ? undefined : { cancel: true };
  });
  pi.on("session_branch", async (event: Frame, ctx: any) => {
    lastAgentEndTerminal = undefined;
    lifecycle("session_branch", event, ctx);
  });
  pi.on("session_shutdown", async (event: Frame, ctx: any) => {
    shuttingDown = true;
    if (reconnectTimer) clearTimeout(reconnectTimer);
    if (keepaliveTimer) clearInterval(keepaliveTimer);
    lifecycle("session_shutdown", event, ctx);
    close();
  });
  pi.on("title_change", async (event: Frame, ctx: any) => lifecycle("title_change", event, ctx));
  pi.on("agent_start", async (event: Frame, ctx: any) => {
    turnGeneration += 1;
    lastAgentEndTerminal = undefined;
    lifecycle("agent_start", event, ctx);
  });
  pi.on("tool_execution_start", async (event: Frame, ctx: any) => lifecycle("tool_execution_start", event, ctx));
  pi.on("tool_execution_update", async (event: Frame, ctx: any) => lifecycle("tool_execution_update", event, ctx));
  pi.on("tool_execution_end", async (event: Frame, ctx: any) => lifecycle("tool_execution_end", event, ctx));
  pi.on("message_start", async (event: Frame, ctx: any) => lifecycle("message_start", event, ctx));
  pi.on("message_end", async (event: Frame, ctx: any) => lifecycle("message_end", event, ctx));
  pi.on("message_update", async (event: Frame, ctx: any) => lifecycle("message_update", event, ctx));
  pi.on("agent_end", async (event: Frame, ctx: any) => {
    lastAgentEndTerminal = agentEndIsTerminal(event);
    lifecycle("agent_end", event, ctx);
  });
  pi.on("session_stop", async (event: Frame, ctx: any) => lifecycle("session_stop", event, ctx));
}
