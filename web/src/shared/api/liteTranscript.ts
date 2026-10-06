/**
 * Lite transcript pages (`detail=lite`, server `zerg/services/transcript_lite.py`)
 * carry every event and all conversation text, but send each tool body as the
 * preview its collapsed row shows, each tool presentation once per page, and
 * omit fields at their default value. Hydration rebuilds the full event shape
 * here, at the fetch boundary, so the rest of the app never sees the wire form.
 *
 * An event whose tool input or output was cut keeps a `lite_body` handle; the
 * expanded row fetches the full body with it (`features/session/liteBodies.ts`).
 */
import type {
  AgentEvent,
  AgentSessionProjectionItem,
  AgentSessionProjectionResponse,
  AgentSessionWorkspaceResponse,
  AgentToolPresentation,
} from "./agents";

type WireEvent = Partial<AgentEvent> & {
  id: AgentEvent["id"];
  role: string;
  tool_presentation_ref?: string;
  tool_presentation_input?: "same" | { value: unknown };
  tool_presentation_shell_summary?: AgentToolPresentation["shell_summary"];
  tool_presentation_children?: AgentToolPresentation["children"];
};

type WireProjection = AgentSessionProjectionResponse & {
  detail?: "lite" | "full";
  tool_presentations?: Record<string, Omit<AgentToolPresentation, "tool_input_json" | "shell_summary" | "children">>;
};

function hydrateEvent(
  wire: WireEvent,
  { timestamp, sessionId, presentations }: { timestamp: string; sessionId: string; presentations: WireProjection["tool_presentations"] },
): AgentEvent {
  const {
    tool_presentation_ref: ref,
    tool_presentation_input: presentedInput,
    tool_presentation_shell_summary: shellSummary,
    tool_presentation_children: children,
    ...fields
  } = wire;
  const base = ref ? presentations?.[ref] : undefined;
  const toolInput = fields.tool_input_json ?? null;
  const presentation: AgentToolPresentation | null = base
    ? ({
        ...base,
        tool_input_json:
          presentedInput === "same" ? toolInput : presentedInput && typeof presentedInput === "object" ? presentedInput.value : null,
        shell_summary: shellSummary ?? null,
        children: children ?? [],
      } as AgentToolPresentation)
    : null;
  const cut = Boolean(fields.tool_input_truncated || fields.tool_output_truncated);
  return {
    content_text: null,
    interaction_kind: null,
    raw_content_text: null,
    input_origin: null,
    turn_end: null,
    tool_name: null,
    tool_output_text: null,
    tool_call_id: null,
    tool_call_state: null,
    in_active_context: true,
    branch_id: null,
    is_head_branch: true,
    event_origin: "durable",
    provisional_state: null,
    provisional_cursor: null,
    provisional_complete: false,
    reconciled_event_id: null,
    tool_output_truncated: false,
    tool_output_original_chars: null,
    media_refs: [],
    timestamp,
    ...fields,
    tool_input_json: toolInput,
    tool_presentation: presentation,
    ...(cut && fields.cursor ? { lite_body: { session_id: sessionId, cursor: fields.cursor } } : {}),
  } as AgentEvent;
}

/** Return the full projection shape for a lite page; a full page is returned as is. */
export function hydrateLiteProjection(projection: AgentSessionProjectionResponse): AgentSessionProjectionResponse {
  const wire = projection as WireProjection | undefined;
  if (wire?.detail !== "lite") return projection;
  const { detail: _detail, tool_presentations: presentations, ...rest } = wire;
  const items: AgentSessionProjectionItem[] = wire.items.map((item) => {
    const sessionId = item.session_id ?? wire.focus_session_id;
    return {
      action: null,
      continued_from_session_id: null,
      continuation_kind: null,
      origin_label: null,
      parent_origin_label: null,
      parent_continuation_kind: null,
      branched_from_event_id: null,
      ...item,
      session_id: sessionId,
      event: item.event
        ? hydrateEvent(item.event as WireEvent, { timestamp: item.timestamp, sessionId, presentations })
        : (item.event ?? null),
    };
  });
  return { ...rest, items };
}

/** Return the full workspace shape for a lite workspace; a full one is returned as is. */
export function hydrateLiteWorkspace(workspace: AgentSessionWorkspaceResponse): AgentSessionWorkspaceResponse {
  if (!workspace?.projection) return workspace;
  const projection = hydrateLiteProjection(workspace.projection);
  if (projection === workspace.projection) return workspace;
  const thread = workspace.thread.sessions
    ? workspace.thread
    : { ...workspace.thread, sessions: [workspace.session] };
  return { ...workspace, thread, projection };
}
