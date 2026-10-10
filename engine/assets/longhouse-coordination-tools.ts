// The Longhouse coordination tools for the OMP and Pi Helm extensions.
//
// One source for both: scripts/generate/generate_coordination_contract.py
// inlines this file into each extension (they ship as single files), right
// after the generated COORDINATION_CONTRACT block it reads. Names,
// descriptions and schemas come from that contract; this binds each tool to
// its Runtime Host call. Edit this file, then run the generator.

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

const coordinationIsRecord = (value: unknown): value is Record<string, unknown> =>
  Boolean(value) && typeof value === "object" && !Array.isArray(value);

type CoordinationToolName =
  (typeof COORDINATION_CONTRACT)["tools"][number]["name"];

const coordinationTool = (name: CoordinationToolName) => {
  const tool = COORDINATION_CONTRACT.tools.find((entry) => entry.name === name);
  if (!tool) throw new Error(`coordination contract has no tool ${name}`);
  return tool;
};

/**
 * One peer as `<session_id> <provider> <state> <age> · <title>`, the same line
 * the engine and Python MCP servers return: the full id tail/send need, what
 * the session is doing, time since its last event, and its title.
 */
export function peerLine(item: Record<string, unknown>, nowMs: number): string {
  const text = (value: unknown) => (typeof value === "string" ? value : "");
  const at = Date.parse(text(item.last_event_at));
  let age = "?";
  if (Number.isFinite(at)) {
    const minutes = Math.max(0, Math.floor((nowMs - at) / 60_000));
    age =
      minutes < 1
        ? "now"
        : minutes < 60
          ? `${minutes}m`
          : minutes < 1440
            ? `${Math.floor(minutes / 60)}h`
            : `${Math.floor(minutes / 1440)}d`;
  }
  const title = text(item.summary_title)
    // eslint-disable-next-line no-control-regex
    .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f]/g, "")
    .split(/\s+/)
    .filter(Boolean)
    .join(" ")
    .slice(0, 80);
  const line = `${text(item.session_id)} ${text(item.provider) || "?"} ${text(item.presence_state) || "?"} ${age}`;
  return title ? `${line} · ${title}` : line;
}

export type LonghouseCoordinationOptions = {
  runtimeUrl: string;
  sessionId: string;
  /** Read on every call: authority can arrive after launch. */
  token: () => string;
  /** A native subagent never borrows its parent's authority. */
  isSubagent: (ctx: unknown) => boolean;
};

export function registerLonghouseCoordination(
  pi: any,
  settings: LonghouseCoordinationOptions,
): void {
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
    if (!settings.runtimeUrl) {
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
      [CURRENT_SESSION_HEADER]: settings.sessionId,
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
        const response = await fetch(`${settings.runtimeUrl}${path}`, {
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
        const errorBody = coordinationIsRecord(value) ? { ...value } : { detail: value };
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

  // Names, descriptions and schemas come from the generated contract; this
  // extension only binds each tool to its API call.
  const coordination = (
    name: CoordinationToolName,
    execute: (params: ToolParams, signal?: AbortSignal) => Promise<unknown>,
  ) => {
    if (typeof pi.registerTool !== "function") return;
    const tool = coordinationTool(name);
    pi.registerTool({
      name,
      label: `Longhouse ${name}`,
      description: tool.description,
      parameters: tool.inputSchema,
      // Native OMP's ToolDefinition.execute passes ctx as its fifth argument:
      // https://github.com/can1357/oh-my-pi/blob/v18.4.5/packages/coding-agent/src/extensibility/extensions/wrapper.ts#L106-L134
      async execute(
        _toolCallId: string,
        params: ToolParams,
        signal: AbortSignal,
        _onUpdate: unknown,
        ctx: unknown,
      ) {
        if (settings.isSubagent(ctx)) {
          return result(
            {
              error:
                "Native subagents cannot use their parent's Longhouse coordination authority. Contact your parent with write agent://Main.",
            },
            true,
          );
        }
        try {
          const value = await execute(params ?? {}, signal);
          return result(value, coordinationIsRecord(value) && "error" in value);
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
    async (params, signal) => {
      let repo = typeof params.repo === "string" ? params.repo.trim() : "";
      if (!repo) {
        const current = await api(`/api/agents/sessions/${settings.sessionId}`, {
          token: settings.token(),
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
        token: settings.token(),
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
            .filter((item) => String(item.session_id ?? "") !== settings.sessionId)
            .filter((item) => !activeOnly || item.has_live_presence)
        : [];
      const peers = sessions.map((item) => peerLine(item, Date.now()));
      return {
        repo,
        active_only: activeOnly,
        total: peers.length,
        peers,
      };
    },
  );

  coordination(
    "search_sessions",
    async (params, signal) => {
      const query = new URLSearchParams();
      if (typeof params.query === "string" && params.query.trim())
        query.set("query", params.query.trim());
      if (typeof params.project === "string" && params.project.trim())
        query.set("project", params.project.trim());
      if (typeof params.provider === "string" && params.provider.trim())
        query.set("provider", params.provider.trim());
      // Omitted days_back means all history for a query and the recent
      // window for a listing; the API applies that, so only a given value is sent.
      if (params.days_back !== undefined && params.days_back !== null)
        query.set(
          "days_back",
          String(Math.max(1, Math.min(90, Number(params.days_back) || 14))),
        );
      query.set(
        "limit",
        String(Math.max(1, Math.min(100, Number(params.limit) || 10))),
      );
      const response = await api(`/api/agents/sessions?${query}`, {
        token: settings.token(),
        signal,
      });
      return response.value;
    },
  );

  const bounded = (value: unknown, fallback: number, min: number, max: number) => {
    const given =
      typeof value === "number" || (typeof value === "string" && value.trim() !== "")
        ? Number(value)
        : Number.NaN;
    return String(Math.max(min, Math.min(max, Number.isFinite(given) ? given : fallback)));
  };

  coordination(
    "recall",
    async (params, signal) => {
      const text = typeof params.query === "string" ? params.query.trim() : "";
      if (!text) return { error: "recall requires a non-empty query" };
      const mode = ["auto", "lexical", "semantic"].includes(String(params.mode))
        ? String(params.mode)
        : "auto";
      const query = new URLSearchParams({ query: text, mode });
      if (typeof params.project === "string" && params.project.trim())
        query.set("project", params.project.trim());
      if (typeof params.provider === "string" && params.provider.trim())
        query.set("provider", params.provider.trim());
      query.set("since_days", bounded(params.since_days, 90, 1, 365));
      query.set("max_results", bounded(params.max_results, 5, 1, 10));
      const response = await api(`/api/agents/recall?${query}`, {
        token: settings.token(),
        signal,
      });
      return response.value;
    },
  );

  coordination(
    "recall_context",
    async (params, signal) => {
      const ref = typeof params.ref === "string" ? params.ref.trim() : "";
      if (!ref) return { error: "recall_context requires ref" };
      const query = new URLSearchParams({
        ref,
        before: bounded(params.before, 2, 0, 5),
        after: bounded(params.after, 2, 0, 5),
        max_content_bytes: bounded(params.max_content_bytes, 1200, 200, 4000),
      });
      const response = await api(`/api/agents/recall/context?${query}`, {
        token: settings.token(),
        signal,
      });
      return response.value;
    },
  );

  coordination(
    "tail",
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
          token: settings.token(),
          signal,
        },
      );
      return response.value;
    },
  );

  coordination(
    "send",
    async (params, signal) => {
      const sessionId = String(params.session_id ?? "").trim();
      const clientRequestId = String(params.client_request_id ?? "").trim();
      if (!sessionId || !clientRequestId)
        return { error: "send requires session_id and client_request_id" };
      const response = await api("/api/agents/directed-inputs", {
        method: "POST",
        token: settings.token(),
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
        token: settings.token(),
        signal,
      });
      return response.value;
    },
  );

  coordination(
    "reply",
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
          token: settings.token(),
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
}
