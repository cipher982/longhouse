import { readFileSync } from "node:fs";
import { connect, type Socket } from "node:net";
import { resolve, sep } from "node:path";

type ImageContent = { type: "image"; data: string; mimeType: string };
type TextContent = { type: "text"; text: string };

/// Longhouse stages image attachments on disk and sends only their path and
/// MIME type over the socket; the bytes are read here, inside the provider
/// process, so the frame cap never applies to image data. Without
/// attachments the message is the plain string OMP has always received.
const userContent = (
  text: string,
  attachments: unknown,
): string | (TextContent | ImageContent)[] => {
  if (!Array.isArray(attachments) || attachments.length === 0) return text;
  const images: ImageContent[] = [];
  for (const item of attachments) {
    if (!item || typeof item !== "object") continue;
    const path = (item as { path?: unknown }).path;
    const mimeType = (item as { mime_type?: unknown }).mime_type;
    if (typeof path !== "string" || typeof mimeType !== "string") continue;
    images.push({
      type: "image",
      data: readFileSync(path).toString("base64"),
      mimeType,
    });
  }
  if (images.length === 0) return text;
  const parts: (TextContent | ImageContent)[] = [];
  if (text.trim()) parts.push({ type: "text", text });
  parts.push(...images);
  return parts;
};

type Frame = Record<string, unknown>;

/// OMP's session transcript suffix. A session's artifacts — and every subagent
/// session it opens — live in the sibling directory named after the file.
const SESSION_FILE_SUFFIX = ".jsonl";

const socketPath = process.env.LONGHOUSE_OMP_HELM_CHANNEL_PATH;
const authToken = process.env.LONGHOUSE_OMP_HELM_CHANNEL_TOKEN;
const launchSessionId = process.env.LONGHOUSE_MANAGED_SESSION_ID;
const initialPrompt = process.env.LONGHOUSE_OMP_HELM_INITIAL_PROMPT ?? "";
const initialPromptDeliveredAtLaunch =
  process.env.LONGHOUSE_OMP_HELM_INITIAL_PROMPT_DELIVERED === "1";
const MAX_FRAME_BYTES = 512 * 1024;
const MAX_METADATA_STRING_LENGTH = 256;
const MAX_LIVE_TEXT_DELTA_LENGTH = 4096;
const CURRENT_SESSION_HEADER = "X-Longhouse-Session-Id";
const COORDINATION_MAX_429_RETRIES = 3;
const COORDINATION_OPERATION_TIMEOUT_MS = 15_000;
const COORDINATION_DEFAULT_RETRY_MS = 1_000;

type ToolParams = Record<string, unknown>;

type ToolResult = {
  content: Array<{ type: "text"; text: string }>;
  details?: Record<string, unknown>;
  isError?: boolean;
};

const isRecord = (value: unknown): value is Record<string, unknown> =>
  Boolean(value) && typeof value === "object" && !Array.isArray(value);

const jsonSchema = (properties: Record<string, unknown>) => ({
  type: "object",
  properties,
});

if (!socketPath || !authToken || !launchSessionId) {
  throw new Error(
    "Longhouse OMP Helm extension is missing launch-scoped channel identity",
  );
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
    if (event[key] !== undefined && typeof event[key] !== "boolean")
      return false;
  }
  if (typeof event.isTerminal === "boolean") return event.isTerminal;
  if (typeof event.willContinue === "boolean") return !event.willContinue;
  return true;
}

/// Every session file this process has spoken for.
///
/// The first context to emit an event owns this launch, and a session file
/// inside that owner's artifacts directory is a subagent of it: OMP opens a
/// child session at `<parent file without .jsonl>/<title>.jsonl` and emits the
/// same extension events from the child's context, whose `sessionManager` is
/// the child's. Those frames are not this session, and the launcher reads an
/// authenticated frame that reports another native session id as the provider
/// moving its own session — so a subagent's frames made the managed session's
/// identity flip between the agent and its children, one INFO line per flip,
/// and its `session_start` reconnected the launch's channel onto the child's
/// context. A file learned later is still learned: a session that compacts or
/// switches reports a new file, and the launcher is the authority on following
/// that move.
const ownedSessionFiles = new Set<string>();

/// The session file a provider context is speaking for, if it names one.
const sessionFileOf = (ctx: unknown): string | undefined => {
  if (typeof ctx !== "object" || ctx === null || !("sessionManager" in ctx)) {
    return undefined;
  }
  const manager = ctx.sessionManager;
  if (
    typeof manager !== "object" ||
    manager === null ||
    !("getSessionFile" in manager)
  ) {
    return undefined;
  }
  const getSessionFile = manager.getSessionFile;
  if (typeof getSessionFile !== "function") return undefined;
  const file: unknown = getSessionFile.call(manager);
  return typeof file === "string" && file.trim()
    ? resolve(file.trim())
    : undefined;
};

/// Whether a provider context belongs to a session OMP opened *under* the
/// session this launch owns, rather than to the session itself.
///
/// Only a proven child is refused. An unrecognized file stays this session:
/// the launcher is the authority on a provider moving its own session, and
/// refusing an unknown file here would silence a live one.
function subagentSessionContext(ctx: unknown): boolean {
  const file = sessionFileOf(ctx);
  if (!file || ownedSessionFiles.has(file)) return false;
  for (const owned of ownedSessionFiles) {
    const artifactsDir = owned.endsWith(SESSION_FILE_SUFFIX)
      ? owned.slice(0, -SESSION_FILE_SUFFIX.length)
      : owned;
    if (file.startsWith(`${artifactsDir}${sep}`)) return true;
  }
  return false;
}

export default function (pi: any) {
  const runtimeUrl = (process.env.LONGHOUSE_OMP_HELM_URL ?? "")
    .trim()
    .replace(/\/+$/, "");
  let coordinationToken = (
    process.env.LONGHOUSE_COORDINATION_TOKEN ?? ""
  ).trim();

  const result = (value: unknown, isError = false): ToolResult => ({
    content: [{ type: "text", text: JSON.stringify(value) }],
    details: {},
    ...(isError ? { isError: true } : {}),
  });

  const api = async (
    path: string,
    options: {
      method?: "GET" | "POST";
      token?: string;
      body?: Record<string, unknown>;
      signal?: AbortSignal;
    } = {},
  ): Promise<{ value: unknown; ok: boolean }> => {
    if (!runtimeUrl) {
      return {
        value: { error: "Longhouse Runtime Host URL is unavailable" },
        ok: false,
      };
    }
    const token = options.token?.trim();
    if (!token) {
      return {
        value: {
          error:
            "This coordination authority is unavailable for the managed session",
        },
        ok: false,
      };
    }
    const headers: Record<string, string> = {
      Accept: "application/json",
      "X-Agents-Token": token,
      [CURRENT_SESSION_HEADER]: launchSessionId,
    };
    if (options.body) headers["Content-Type"] = "application/json";
    const deadline = Date.now() + COORDINATION_OPERATION_TIMEOUT_MS;
    const body = options.body ? JSON.stringify(options.body) : undefined;
    const method = options.method ?? "GET";
    const retrySafe =
      method === "GET" ||
      (typeof options.body?.client_request_id === "string" &&
        options.body.client_request_id.trim().length > 0);
    for (
      let attempt = 0;
      attempt <= COORDINATION_MAX_429_RETRIES;
      attempt += 1
    ) {
      if (options.signal?.aborted) {
        return {
          value: { error: "Coordination request was cancelled" },
          ok: false,
        };
      }
      const controller = new AbortController();
      const abort = () => controller.abort();
      const timeout = setTimeout(
        () => controller.abort(),
        Math.max(1, deadline - Date.now()),
      );
      options.signal?.addEventListener("abort", abort, { once: true });
      try {
        const response = await fetch(`${runtimeUrl}${path}`, {
          method,
          headers,
          body,
          signal: controller.signal,
        });
        const text = await response.text();
        let value: unknown;
        try {
          value = text ? JSON.parse(text) : {};
        } catch {
          value = { error: text.slice(0, 300) };
        }
        if (response.ok) return { value, ok: true };
        const retryAfter = response.headers.get("Retry-After");
        const delayMs = retryAfter
          ? (() => {
              const seconds = Number(retryAfter);
              if (Number.isFinite(seconds))
                return Math.max(
                  0,
                  Math.min(seconds * 1000, COORDINATION_OPERATION_TIMEOUT_MS),
                );
              const timestamp = Date.parse(retryAfter);
              return Number.isFinite(timestamp)
                ? Math.max(
                    0,
                    Math.min(
                      timestamp - Date.now(),
                      COORDINATION_OPERATION_TIMEOUT_MS,
                    ),
                  )
                : COORDINATION_DEFAULT_RETRY_MS;
            })()
          : COORDINATION_DEFAULT_RETRY_MS;
        if (
          response.status === 429 &&
          retrySafe &&
          attempt < COORDINATION_MAX_429_RETRIES &&
          Date.now() + delayMs < deadline
        ) {
          await new Promise<void>((resolve) => {
            const finish = () => {
              clearTimeout(timer);
              options.signal?.removeEventListener("abort", finish);
              resolve();
            };
            const timer = setTimeout(finish, delayMs);
            options.signal?.addEventListener("abort", finish, { once: true });
          });
          continue;
        }
        const errorBody = isRecord(value) ? { ...value } : { detail: value };
        if (!("error" in errorBody))
          errorBody.error = `API returned ${response.status}`;
        return {
          value: {
            ...errorBody,
            status: response.status,
            ...(retryAfter ? { retry_after: retryAfter } : {}),
          },
          ok: false,
        };
      } catch (error) {
        return {
          value: {
            error: error instanceof Error ? error.message : String(error),
          },
          ok: false,
        };
      } finally {
        clearTimeout(timeout);
        options.signal?.removeEventListener("abort", abort);
      }
    }
    return {
      value: { error: "Coordination request retry budget exhausted" },
      ok: false,
    };
  };

  const coordination = (
    name: string,
    description: string,
    parameters: Record<string, unknown>,
    execute: (params: ToolParams, signal?: AbortSignal) => Promise<unknown>,
  ) => {
    if (typeof pi.registerTool !== "function") return;
    pi.registerTool({
      name,
      label: `Longhouse ${name}`,
      description,
      parameters: jsonSchema(parameters),
      async execute(
        _toolCallId: string,
        params: ToolParams,
        signal: AbortSignal,
      ) {
        try {
          const value = await execute(params ?? {}, signal);
          return result(value, isRecord(value) && "error" in value);
        } catch (error) {
          return result(
            { error: error instanceof Error ? error.message : String(error) },
            true,
          );
        }
      },
    });
  };

  coordination(
    "peers",
    "List same-repository Longhouse collaborators. This is a liveness view, not transcript history.",
    {
      repo: {
        type: "string",
        description:
          "Repository path or name. Omit to infer it from this session.",
      },
      active_only: {
        type: "boolean",
        description: "Only include peers with live presence (default true).",
      },
    },
    async (params, signal) => {
      let repo = typeof params.repo === "string" ? params.repo.trim() : "";
      if (!repo) {
        const current = await api(`/api/agents/sessions/${launchSessionId}`, {
          token: coordinationToken,
          signal,
        });
        if (current.ok && current.value && typeof current.value === "object") {
          const data = current.value as Record<string, unknown>;
          const gitRepo = String(data.git_repo ?? "").trim();
          const cwd = String(data.cwd ?? "").trim();
          repo = gitRepo || cwd;
        }
      }
      if (!repo)
        return {
          error:
            "peers requires repo or a current session with git_repo or cwd",
        };
      const query = new URLSearchParams({
        repo,
        days: "7",
        include_automation: "true",
      });
      const response = await api(`/api/agents/sessions/wall?${query}`, {
        token: coordinationToken,
        signal,
      });
      if (!response.ok || !response.value || typeof response.value !== "object")
        return response.value;
      const activeOnly = params.active_only !== false;
      const sessions = Array.isArray(
        (response.value as Record<string, unknown>).sessions,
      )
        ? (
            (response.value as Record<string, unknown>).sessions as Array<
              Record<string, unknown>
            >
          )
            .filter((item) => String(item.session_id ?? "") !== launchSessionId)
            .filter((item) => !activeOnly || item.has_live_presence)
        : [];
      return {
        repo,
        active_only: activeOnly,
        peers: sessions,
        total: sessions.length,
      };
    },
  );

  coordination(
    "search_sessions",
    "Find past Longhouse sessions by transcript content, or list recent sessions when query is omitted.",
    {
      query: {
        type: "string",
        description: "Text to match; omit to list recent sessions.",
      },
      project: { type: "string" },
      provider: { type: "string" },
      days_back: { type: "integer", description: "1-90, default 14." },
      limit: { type: "integer", description: "1-100, default 10." },
    },
    async (params, signal) => {
      const query = new URLSearchParams();
      if (typeof params.query === "string" && params.query.trim())
        query.set("query", params.query.trim());
      if (typeof params.project === "string" && params.project.trim())
        query.set("project", params.project.trim());
      if (typeof params.provider === "string" && params.provider.trim())
        query.set("provider", params.provider.trim());
      query.set(
        "days_back",
        String(Math.max(1, Math.min(90, Number(params.days_back) || 14))),
      );
      query.set(
        "limit",
        String(Math.max(1, Math.min(100, Number(params.limit) || 10))),
      );
      const response = await api(`/api/agents/sessions?${query}`, {
        token: coordinationToken,
        signal,
      });
      return response.value;
    },
  );

  coordination(
    "tail",
    "Read recent events from a Longhouse session transcript. Prefer roles user,assistant to avoid tool noise.",
    {
      session_id: { type: "string" },
      limit: { type: "integer", description: "1-100, default 30." },
      roles: { type: "string" },
      max_content_chars: {
        type: "integer",
        description: "200-100000 per event, default 4000.",
      },
    },
    async (params, signal) => {
      const sessionId = String(params.session_id ?? "").trim();
      if (!sessionId) return { error: "tail requires session_id" };
      const query = new URLSearchParams({
        limit: String(Math.max(1, Math.min(100, Number(params.limit) || 30))),
        max_content_chars: String(
          Math.max(
            200,
            Math.min(100000, Number(params.max_content_chars) || 4000),
          ),
        ),
      });
      if (typeof params.roles === "string" && params.roles.trim())
        query.set("roles", params.roles.trim());
      const response = await api(
        `/api/agents/sessions/${encodeURIComponent(sessionId)}/tail?${query}`,
        {
          token: coordinationToken,
          signal,
        },
      );
      return response.value;
    },
  );

  coordination(
    "send",
    "Send durable attributed input to another managed Longhouse session. Keep client_request_id stable across retries.",
    {
      session_id: { type: "string" },
      text: {
        type: "string",
        description: "Peer message, maximum 4000 characters.",
      },
      client_request_id: {
        type: "string",
        description: "Stable caller-owned idempotency key.",
      },
    },
    async (params, signal) => {
      const sessionId = String(params.session_id ?? "").trim();
      const clientRequestId = String(params.client_request_id ?? "").trim();
      if (!sessionId || !clientRequestId)
        return { error: "send requires session_id and client_request_id" };
      const response = await api("/api/agents/directed-inputs", {
        method: "POST",
        token: coordinationToken,
        signal,
        body: {
          target_session_id: sessionId,
          text: String(params.text ?? ""),
          client_request_id: clientRequestId,
        },
      });
      return response.value;
    },
  );

  coordination(
    "inbox",
    "Recover durable directed input for this managed Longhouse session after compaction or missed live delivery.",
    {
      direction: { type: "string", enum: ["inbound", "outbound", "all"] },
      after_cursor: {
        type: "integer",
        description: "Return ids greater than this cursor.",
      },
      limit: { type: "integer", description: "1-200, default 20." },
    },
    async (params, signal) => {
      const direction = ["inbound", "outbound", "all"].includes(
        String(params.direction),
      )
        ? String(params.direction)
        : "inbound";
      const query = new URLSearchParams({
        direction,
        after_id: String(Math.max(0, Number(params.after_cursor) || 0)),
        limit: String(Math.max(1, Math.min(200, Number(params.limit) || 20))),
      });
      const response = await api(`/api/agents/directed-inputs?${query}`, {
        token: coordinationToken,
        signal,
      });
      return response.value;
    },
  );

  coordination(
    "reply",
    "Reply to an inbound Longhouse directed input without copying its source session id. Keep client_request_id stable across retries.",
    {
      input_id: { type: "integer" },
      text: {
        type: "string",
        description: "Reply body, maximum 4000 characters.",
      },
      client_request_id: {
        type: "string",
        description: "Stable caller-owned idempotency key.",
      },
    },
    async (params, signal) => {
      const inputId = Number(params.input_id);
      const clientRequestId = String(params.client_request_id ?? "").trim();
      if (!Number.isInteger(inputId) || inputId < 1 || !clientRequestId) {
        return {
          error: "reply requires a positive input_id and client_request_id",
        };
      }
      const response = await api(
        `/api/agents/directed-inputs/${inputId}/reply`,
        {
          method: "POST",
          token: coordinationToken,
          signal,
          body: {
            text: String(params.text ?? ""),
            client_request_id: clientRequestId,
          },
        },
      );
      return response.value;
    },
  );

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
    if (
      !socket ||
      !ready ||
      expectedGeneration !== generation ||
      socket.destroyed
    )
      return false;
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
      type:
        typeof event.type === "string"
          ? event.type.slice(0, MAX_METADATA_STRING_LENGTH)
          : kind,
    };
    for (const key of [
      "reason",
      "request_id",
      "turn_id",
      "run_id",
      "title",
      "toolName",
      "toolCallId",
    ]) {
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
    if (
      kind === "message_update" &&
      event.assistantMessageEvent &&
      typeof event.assistantMessageEvent === "object"
    ) {
      const update = event.assistantMessageEvent as Frame;
      if (typeof update.type === "string") {
        compact.message_event_type = update.type.slice(
          0,
          MAX_METADATA_STRING_LENGTH,
        );
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
    // The prompt belongs to this launch's session, never to a subagent's.
    if (subagentSessionContext(ctx)) return;
    if (initialPromptAttempts >= INITIAL_PROMPT_MAX_ATTEMPTS) return;
    initialPromptAttempts += 1;
    sendEvent(
      "initial_prompt_request",
      { type: "initial_prompt_request" },
      ctx,
    );
    setTimeout(() => requestInitialPrompt(ctx), INITIAL_PROMPT_RETRY_MS);
  };

  const handleFrame = (frame: Frame, ctx: any, ownGeneration: number) => {
    if (ownGeneration !== generation) return;
    if (frame.kind === "extension_ready") {
      if (frame.ok !== true)
        throw new Error(
          String(
            (frame.error as Frame | undefined)?.message ??
              "OMP Helm handshake rejected",
          ),
        );
      connectionId =
        typeof frame.connection_id === "string" ? frame.connection_id : "";
      leaseGeneration =
        typeof frame.lease_generation === "string"
          ? frame.lease_generation
          : "";
      ready = Boolean(connectionId && leaseGeneration);
      return;
    }
    if (frame.kind === "extension_generation") {
      connectionId =
        typeof frame.connection_id === "string"
          ? frame.connection_id
          : connectionId;
      leaseGeneration =
        typeof frame.lease_generation === "string"
          ? frame.lease_generation
          : leaseGeneration;
      ready = Boolean(connectionId && leaseGeneration);
      const waiters = generationWaiters.splice(0);
      for (const done of waiters) done();
      return;
    }
    if (frame.kind === "coordination_authority") {
      coordinationToken =
        typeof frame.token === "string" ? frame.token.trim() : "";
      return;
    }
    if (frame.kind === "initial_prompt_grant") {
      if (
        frame.granted === true &&
        initialPrompt?.trim() &&
        !initialPromptDelivered
      ) {
        initialPromptDelivered = true;
        void Promise.resolve(pi.sendUserMessage(initialPrompt)).catch(
          (error: unknown) => {
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
          },
        );
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
    reconnectTimer = setTimeout(
      async () => {
        reconnectTimer = undefined;
        reconnectAttempts += 1;
        try {
          await connectChannel(ctx);
          // A reconnect can cross a provider turn boundary while the channel is
          // down. The turn generation and terminal decision in the snapshot let
          // the launcher distinguish a missed new turn from old drain frames.
          const providerIdle = ompProviderIsIdle(
            lastAgentEndTerminal,
            Boolean(ctx.isIdle()),
          );
          reconnectAttempts = 0;
          sendEvent(
            "session_reconnect",
            { type: "session_reconnect", provider_idle: providerIdle },
            ctx,
          );
        } catch {
          scheduleReconnect(ctx);
        }
      },
      Math.min(1000, 100 * 2 ** Math.max(0, reconnectAttempts - 1)),
    );
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

  const handleCommand = async (
    command: Frame,
    ctx: any,
    ownGeneration: number,
  ) => {
    if (!ctx) return;
    const current = session(ctx);
    const authorityFields = [
      "auth_token",
      "session_id",
      "native_session_id",
      "session_file",
      "connection_id",
      "lease_generation",
    ];
    const authorityMatches = authorityFields.every(
      (field) => command[field] === current[field],
    );
    const kind = String(command.kind);
    const text = typeof command.text === "string" ? command.text : "";
    let reply: Frame = {
      kind: "command_result",
      request_id: command.request_id,
      ok: false,
      ...current,
    };
    try {
      if (!authorityMatches)
        throw new Error("OMP Helm command authority is stale");
      const content = userContent(text, command.attachments);
      if (
        ["send", "steer"].includes(kind) &&
        !text.trim() &&
        typeof content === "string"
      )
        throw new Error("OMP Helm input text must not be empty");
      if (kind === "send") {
        if (providerIsIdle(ctx))
          await Promise.resolve(pi.sendUserMessage(content));
        else
          await Promise.resolve(
            pi.sendUserMessage(content, { deliverAs: "followUp" }),
          );
      } else if (kind === "steer") {
        if (providerIsIdle(ctx))
          throw new Error("OMP provider has no active turn to steer");
        await Promise.resolve(
          pi.sendUserMessage(content, { deliverAs: "steer" }),
        );
      } else if (kind === "abort") {
        await Promise.resolve(ctx.abort());
      } else if (kind === "terminate") {
        await Promise.resolve(ctx.shutdown());
      } else {
        throw new Error(`unknown OMP Helm command: ${kind}`);
      }
      reply = {
        ...reply,
        ok: true,
        status: providerIsIdle(ctx) ? "idle" : "active",
      };
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
    // A subagent's events are not this session's: they would publish the
    // child's identity, activity and title as the managed session's.
    if (subagentSessionContext(ctx)) return false;
    const file = sessionFileOf(ctx);
    if (file) ownedSessionFiles.add(file);
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
    // OMP emits this for every subagent session too. Connecting for one would
    // replace the channel this launch owns with one bound to the child's
    // context, so its keepalives and idle samples would describe the child.
    if (subagentSessionContext(ctx)) return;
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
      commandChain = commandChain.then(() =>
        handleCommand(command, ctx, generation),
      );
    }
  });
  pi.on("session_before_switch", async (event: Frame, ctx: any) => {
    const completed = await waitForReplacement(
      "session_before_switch",
      event,
      ctx,
    );
    return completed ? undefined : { cancel: true };
  });
  pi.on("session_switch", async (event: Frame, ctx: any) => {
    lastAgentEndTerminal = undefined;
    lifecycle("session_switch", event, ctx);
  });
  pi.on("session_before_branch", async (event: Frame, ctx: any) => {
    const completed = await waitForReplacement(
      "session_before_branch",
      event,
      ctx,
    );
    return completed ? undefined : { cancel: true };
  });
  pi.on("session_branch", async (event: Frame, ctx: any) => {
    lastAgentEndTerminal = undefined;
    lifecycle("session_branch", event, ctx);
  });
  pi.on("session_shutdown", async (event: Frame, ctx: any) => {
    // A subagent ending is not this launch's session ending, and closing the
    // channel for it would leave the managed session with no transport.
    if (subagentSessionContext(ctx)) return;
    shuttingDown = true;
    if (reconnectTimer) clearTimeout(reconnectTimer);
    if (keepaliveTimer) clearInterval(keepaliveTimer);
    lifecycle("session_shutdown", event, ctx);
    close();
  });
  pi.on("title_change", async (event: Frame, ctx: any) =>
    lifecycle("title_change", event, ctx),
  );
  pi.on("agent_start", async (event: Frame, ctx: any) => {
    turnGeneration += 1;
    lastAgentEndTerminal = undefined;
    lifecycle("agent_start", event, ctx);
  });
  pi.on("tool_execution_start", async (event: Frame, ctx: any) =>
    lifecycle("tool_execution_start", event, ctx),
  );
  pi.on("tool_execution_update", async (event: Frame, ctx: any) =>
    lifecycle("tool_execution_update", event, ctx),
  );
  pi.on("tool_execution_end", async (event: Frame, ctx: any) =>
    lifecycle("tool_execution_end", event, ctx),
  );
  pi.on("message_start", async (event: Frame, ctx: any) =>
    lifecycle("message_start", event, ctx),
  );
  pi.on("message_end", async (event: Frame, ctx: any) =>
    lifecycle("message_end", event, ctx),
  );
  pi.on("message_update", async (event: Frame, ctx: any) =>
    lifecycle("message_update", event, ctx),
  );
  pi.on("agent_end", async (event: Frame, ctx: any) => {
    lastAgentEndTerminal = agentEndIsTerminal(event);
    lifecycle("agent_end", event, ctx);
  });
  pi.on("session_stop", async (event: Frame, ctx: any) =>
    lifecycle("session_stop", event, ctx),
  );
}
