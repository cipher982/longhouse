import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { connect, type Socket } from "node:net";

type Frame = Record<string, unknown>;

const socketPath = process.env.LONGHOUSE_PI_HELM_CHANNEL_PATH;
const authToken = process.env.LONGHOUSE_PI_HELM_CHANNEL_TOKEN;
const launchSessionId = process.env.LONGHOUSE_MANAGED_SESSION_ID;
const initialPrompt = process.env.LONGHOUSE_PI_HELM_INITIAL_PROMPT;
const MAX_FRAME_BYTES = 512 * 1024;
const MAX_RECONNECT_ATTEMPTS = 5;
const MAX_DEFERRED_COMMANDS = 32;

if (!socketPath || !authToken || !launchSessionId) {
  throw new Error("Longhouse Pi Helm extension is missing launch-scoped channel identity");
}

export default function (pi: ExtensionAPI) {
  let socket: Socket | undefined;
  let buffer = "";
  let generation = 0;
  let ready = false;
  let shuttingDown = false;
  let connectionPromise: Promise<void> | undefined;
  let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
  let reconnectAttempts = 0;
  let connectionId = "";
  let leaseGeneration = "";
  let activityPhase = "unknown";
  let activityTool: string | undefined;
  let commandChain = Promise.resolve();
  let acceptingCommands = false;
  let deferredCommands: Frame[] = [];
  const acknowledgements = new Map<string, { resolve: () => void; reject: (error: Error) => void }>();

  const authorityFrame = (ctx: ExtensionContext): Frame => ({
    auth_token: authToken,
    session_id: launchSessionId,
    provider_session_id: ctx.sessionManager.getSessionId(),
    connection_id: connectionId,
    lease_generation: leaseGeneration,
  });

  const sessionFrame = (event: Frame, ctx: ExtensionContext): Frame => ({
    ...event,
    ...authorityFrame(ctx),
    session_file: ctx.sessionManager.getSessionFile(),
  });

  const write = (frame: Frame, expectedGeneration = generation) => {
    if (!socket || !ready || expectedGeneration !== generation || socket.destroyed) return false;
    const bytes = Buffer.from(`${JSON.stringify(frame)}\n`, "utf8");
    if (bytes.byteLength > MAX_FRAME_BYTES) return false;
    socket.write(bytes);
    return true;
  };

  const closeChannel = () => {
    generation += 1;
    ready = false;
    buffer = "";
    connectionPromise = undefined;
    commandChain = Promise.resolve();
    acceptingCommands = false;
    deferredCommands = [];
    for (const waiter of acknowledgements.values()) {
      waiter.reject(new Error("Pi Helm channel closed"));
    }
    acknowledgements.clear();
    const old = socket;
    socket = undefined;
    old?.end();
    old?.removeAllListeners();
  };

  const handleFrame = (frame: Frame, ctx: ExtensionContext, ownGeneration: number) => {
    if (ownGeneration !== generation) return;
    const kind = frame.kind;
    if (kind === "extension_ready") {
      if (frame.ok !== true) {
        throw new Error(String((frame.error as Frame | undefined)?.message ?? "Pi Helm handshake rejected"));
      }
      connectionId = typeof frame.connection_id === "string" ? frame.connection_id : "";
      leaseGeneration = typeof frame.lease_generation === "string" ? frame.lease_generation : "";
      ready = Boolean(connectionId && leaseGeneration);
      return;
    }
    if (kind === "session_start_ack") {
      const requestId = typeof frame.request_id === "string" ? frame.request_id : "";
      const waiter = acknowledgements.get(requestId);
      if (waiter) {
        acknowledgements.delete(requestId);
        frame.ok === true ? waiter.resolve() : waiter.reject(new Error("Pi Helm session start was rejected"));
      }
      return;
    }
    if (kind === "send" || kind === "steer" || kind === "follow_up" || kind === "abort" || kind === "terminate") {
      if (!acceptingCommands) {
        if (deferredCommands.length < MAX_DEFERRED_COMMANDS) deferredCommands.push(frame);
        return;
      }
      commandChain = commandChain
        .then(() => ownGeneration === generation ? handleCommand(frame, ctx) : undefined)
        .catch(() => undefined);
    }
  };

  const connectChannel = (ctx: ExtensionContext): Promise<void> => {
    if (connectionPromise) return connectionPromise;
    const ownGeneration = ++generation;
    buffer = "";
    ready = false;
    connectionPromise = new Promise<void>((resolve, reject) => {
      const candidate = connect(socketPath, () => {
        const hello: Frame = {
          kind: "extension_hello",
          auth_token: authToken,
          session_id: launchSessionId,
          provider_session_id: ctx.sessionManager.getSessionId(),
          session_file: ctx.sessionManager.getSessionFile(),
        };
        const bytes = Buffer.from(`${JSON.stringify(hello)}\n`, "utf8");
        candidate.write(bytes);
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
          fail(new Error("Pi Helm channel frame exceeds limit"));
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
            if (ready && connectionPromise) resolve();
          } catch (error) {
            fail(error instanceof Error ? error : new Error(String(error)));
            candidate.destroy();
            return;
          }
        }
      });
      candidate.once("error", (error) => fail(error));
      candidate.once("close", () => {
        if (ownGeneration !== generation) return;
        ready = false;
        socket = undefined;
        connectionPromise = undefined;
        if (!shuttingDown) scheduleReconnect(ctx);
      });
    });
    return connectionPromise;
  };

  const sendSessionStart = async (reason: string, ctx: ExtensionContext) => {
    const requestId = `${generation}:${Date.now()}`;
    const event = sessionFrame({ kind: "session_start", reason, request_id: requestId }, ctx);
    await new Promise<void>((resolve, reject) => {
      acknowledgements.set(requestId, { resolve, reject });
      if (!write(event)) {
        acknowledgements.delete(requestId);
        reject(new Error("Pi Helm channel is not ready"));
      }
    });
  };

  const acceptDeferredCommands = (ctx: ExtensionContext) => {
    acceptingCommands = true;
    const queued = deferredCommands;
    deferredCommands = [];
    for (const command of queued) {
      commandChain = commandChain
        .then(() => handleCommand(command, ctx))
        .catch(() => undefined);
    }
  };

  const scheduleReconnect = (ctx: ExtensionContext) => {
    if (shuttingDown || reconnectTimer || reconnectAttempts >= MAX_RECONNECT_ATTEMPTS) return;
    reconnectTimer = setTimeout(async () => {
      reconnectTimer = undefined;
      reconnectAttempts += 1;
      try {
        await connectChannel(ctx);
        await sendSessionStart("reconnect", ctx);
        reconnectAttempts = 0;
        emitActivity(ctx.isIdle() ? "idle" : "running", undefined, ctx);
        acceptDeferredCommands(ctx);
      } catch {
        scheduleReconnect(ctx);
      }
    }, Math.min(1000, 100 * 2 ** reconnectAttempts));
  };

  const emitActivity = (phase: string, toolName: string | undefined, ctx: ExtensionContext) => {
    if (phase === activityPhase && toolName === activityTool) return;
    activityPhase = phase;
    activityTool = toolName;
    write(sessionFrame({ kind: "activity", phase, tool_name: toolName }, ctx));
  };

  const handleCommand = async (command: Frame, ctx: ExtensionContext) => {
    const requestId = typeof command.request_id === "string" ? command.request_id : "";
    const kind = command.kind;
    let reply: Frame = { kind: "command_result", request_id: requestId, ok: false };
    try {
      const text = typeof command.text === "string" ? command.text : "";
      if (["send", "steer", "follow_up"].includes(String(kind)) && !text.trim()) {
        throw new Error("Pi Helm input text must not be empty");
      }
      if (kind === "send") {
        if (!ctx.isIdle()) throw new Error("Pi provider is not idle");
        await Promise.resolve(pi.sendUserMessage(text, { expandPromptTemplates: false }));
      } else if (kind === "steer") {
        if (ctx.isIdle()) throw new Error("Pi provider has no active turn to steer");
        await Promise.resolve(pi.sendUserMessage(text, { deliverAs: "steer", expandPromptTemplates: false }));
      } else if (kind === "follow_up") {
        if (ctx.isIdle()) {
          await Promise.resolve(pi.sendUserMessage(text, { expandPromptTemplates: false }));
        } else {
          await Promise.resolve(pi.sendUserMessage(text, {
            deliverAs: "followUp",
            expandPromptTemplates: false,
          }));
        }
      } else if (kind === "abort") {
        await Promise.resolve(ctx.abort());
      } else if (kind === "terminate") {
        await Promise.resolve(ctx.shutdown());
      } else {
        throw new Error(`unknown Pi Helm command: ${String(kind)}`);
      }
      reply = {
        ...reply,
        ok: true,
        provider_session_id: ctx.sessionManager.getSessionId(),
        status: ctx.isIdle() ? "idle" : "active",
      };
    } catch (error) {
      reply.error = {
        code: kind === "steer" && ctx.isIdle() ? "turn_ended" : "command_failed",
        message: error instanceof Error ? error.message : String(error),
      };
    }
    write(sessionFrame(reply, ctx));
  };

  pi.on("session_start", async (event, ctx) => {
    shuttingDown = false;
    reconnectAttempts = 0;
    if (socket) closeChannel();
    await connectChannel(ctx);
    await sendSessionStart(event.reason, ctx);
    emitActivity(ctx.isIdle() ? "idle" : "running", undefined, ctx);
    if (event.reason === "startup" && initialPrompt?.trim()) {
      await Promise.resolve(pi.sendUserMessage(initialPrompt, { expandPromptTemplates: false }));
    }
    acceptDeferredCommands(ctx);
  });

  pi.on("session_shutdown", async (event, ctx) => {
    shuttingDown = true;
    if (reconnectTimer) clearTimeout(reconnectTimer);
    reconnectTimer = undefined;
    if (ready) write(sessionFrame({ kind: "session_shutdown", reason: event.reason }, ctx));
    closeChannel();
  });

  pi.on("agent_start", async (_event, ctx) => emitActivity("running", undefined, ctx));
  pi.on("tool_execution_start", async (event, ctx) => emitActivity("running", event.toolName, ctx));
  pi.on("tool_execution_update", async (event, ctx) => emitActivity("running", event.toolName, ctx));
  pi.on("tool_execution_end", async (_event, ctx) => emitActivity("running", undefined, ctx));
  pi.on("message_update", async (_event, ctx) => emitActivity("thinking", undefined, ctx));
  pi.on("ui_prompt_start", async (_event, ctx) => emitActivity("authority", undefined, ctx));
  pi.on("ui_prompt_end", async (_event, ctx) => emitActivity(ctx.isIdle() ? "idle" : "running", undefined, ctx));
  // agent_end may precede retries, compaction or queued follow-ups.
  pi.on("agent_settled", async (_event, ctx) => {
    emitActivity(ctx.isIdle() ? "idle" : "running", undefined, ctx);
  });
}
