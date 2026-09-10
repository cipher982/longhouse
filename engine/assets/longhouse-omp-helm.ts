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

if (!socketPath || !authToken || !launchSessionId) {
  throw new Error("Longhouse OMP Helm extension is missing launch-scoped channel identity");
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
  let leaseGeneration = "";
  let commandChain = Promise.resolve();
  let initialPromptRequested = false;
  let initialPromptDelivered = initialPromptDeliveredAtLaunch;
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
        compact[key] = typeof event[key] === "boolean" ? event[key] : MALFORMED_BOOLEAN_MARKER;
      }
    }
    for (const key of ["success", "isError"]) {
      if (typeof event[key] === "boolean") compact[key] = event[key];
    }
    if (typeof event.status === "string") compact.status = event.status.slice(0, MAX_METADATA_STRING_LENGTH);
    return compact;
  };

  const sendEvent = (kind: string, event: Frame, ctx: any) =>
    write({ kind, event: compactLifecycleEvent(kind, event), ...session(ctx) });

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
        void Promise.resolve(pi.sendUserMessage(initialPrompt)).catch(() => undefined);
      }
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
        reconnectAttempts = 0;
        sendEvent("session_reconnect", { type: "session_reconnect" }, ctx);
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
        candidate.write(`${JSON.stringify({ kind: "extension_hello", ...session(ctx) })}\n`);
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
        if (ctx.isIdle()) await Promise.resolve(pi.sendUserMessage(text));
        else await Promise.resolve(pi.sendUserMessage(text, { deliverAs: "followUp" }));
      } else if (kind === "steer") {
        if (ctx.isIdle()) throw new Error("OMP provider has no active turn to steer");
        await Promise.resolve(pi.sendUserMessage(text, { deliverAs: "steer" }));
      } else if (kind === "abort") {
        await Promise.resolve(ctx.abort());
      } else if (kind === "terminate") {
        await Promise.resolve(ctx.shutdown());
      } else {
        throw new Error(`unknown OMP Helm command: ${kind}`);
      }
      reply = { ...reply, ok: true, status: ctx.isIdle() ? "idle" : "active" };
    } catch (error) {
      reply.error = {
        code: !authorityMatches
          ? "stale_channel"
          : kind === "steer" && ctx.isIdle()
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
    if (socket) close();
    await connectChannel(ctx);
    lifecycle("session_start", event, ctx);
    if (event.type === "session_start" && initialPrompt?.trim() && !initialPromptRequested) {
      if (sendEvent("initial_prompt_request", { type: "initial_prompt_request" }, ctx)) {
        initialPromptRequested = true;
      }
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
  pi.on("session_switch", async (event: Frame, ctx: any) => lifecycle("session_switch", event, ctx));
  pi.on("session_before_branch", async (event: Frame, ctx: any) => {
    const completed = await waitForReplacement("session_before_branch", event, ctx);
    return completed ? undefined : { cancel: true };
  });
  pi.on("session_branch", async (event: Frame, ctx: any) => lifecycle("session_branch", event, ctx));
  pi.on("session_shutdown", async (event: Frame, ctx: any) => {
    shuttingDown = true;
    if (reconnectTimer) clearTimeout(reconnectTimer);
    lifecycle("session_shutdown", event, ctx);
    close();
  });
  pi.on("title_change", async (event: Frame, ctx: any) => lifecycle("title_change", event, ctx));
  pi.on("agent_start", async (event: Frame, ctx: any) => lifecycle("agent_start", event, ctx));
  pi.on("tool_execution_start", async (event: Frame, ctx: any) => lifecycle("tool_execution_start", event, ctx));
  pi.on("tool_execution_update", async (event: Frame, ctx: any) => lifecycle("tool_execution_update", event, ctx));
  pi.on("tool_execution_end", async (event: Frame, ctx: any) => lifecycle("tool_execution_end", event, ctx));
  pi.on("message_update", async (event: Frame, ctx: any) => lifecycle("message_update", event, ctx));
  pi.on("agent_end", async (event: Frame, ctx: any) => lifecycle("agent_end", event, ctx));
  pi.on("session_stop", async (event: Frame, ctx: any) => lifecycle("session_stop", event, ctx));
}
