import { useContext } from "react";
import { QueryClientContext, useQuery, type QueryClient } from "@tanstack/react-query";

import {
  fetchSessionEventBodies,
  type AgentEvent,
  type SessionEventBody,
  type SessionEventBodiesResponse,
} from "@/shared/api/agents";
import type { ToolInteraction } from "@/shared/session/model/types";

/**
 * A lite transcript page sends a tool call's input and output as the preview
 * its collapsed row shows. Expanding the row loads the full bodies once; they
 * are immutable for a cursor, so they stay cached for the session.
 */
export function hasLiteBodies(interaction: ToolInteraction): boolean {
  return cutEvents(interaction).length > 0;
}

function cutEvents(interaction: ToolInteraction): AgentEvent[] {
  return [interaction.callEvent, interaction.resultEvent].filter(
    (event): event is AgentEvent => Boolean(event?.lite_body),
  );
}

function bodiesQueryOptions(interaction: ToolInteraction) {
  const events = cutEvents(interaction);
  const sessionId = events[0]?.lite_body?.session_id ?? null;
  const cursors = events.map((event) => event.lite_body!.cursor);
  return {
    queryKey: ["agent-session-event-bodies", sessionId, cursors] as const,
    queryFn: () => fetchSessionEventBodies(sessionId!, cursors),
    enabled: sessionId !== null && cursors.length > 0,
    staleTime: Infinity,
    gcTime: 10 * 60_000,
  };
}

function withBody(event: AgentEvent | null, bodies: Map<string, SessionEventBody>): AgentEvent | null {
  const body = event?.lite_body ? bodies.get(event.lite_body.cursor) : undefined;
  if (!event || !body) return event;
  return {
    ...event,
    tool_input_json: body.tool_input_json ?? event.tool_input_json,
    tool_output_text: body.tool_output_text ?? event.tool_output_text,
    tool_presentation: body.tool_presentation ?? event.tool_presentation,
    tool_input_truncated: false,
    tool_output_truncated: false,
    lite_body: undefined,
  };
}

export function mergeEventBodies(
  interaction: ToolInteraction,
  response: SessionEventBodiesResponse | undefined,
): ToolInteraction {
  if (!response?.events.length) return interaction;
  const bodies = new Map(response.events.map((body) => [body.cursor, body]));
  const callEvent = withBody(interaction.callEvent, bodies);
  const resultEvent = withBody(interaction.resultEvent, bodies);
  return {
    ...interaction,
    callEvent,
    resultEvent,
    presentation: callEvent?.tool_presentation ?? interaction.presentation,
  };
}

/** The interaction with full bodies once loaded; the preview interaction until then. */
export function useFullToolInteraction(interaction: ToolInteraction): {
  interaction: ToolInteraction;
  loading: boolean;
  failed: boolean;
} {
  const options = bodiesQueryOptions(interaction);
  const { data, isLoading, isError } = useQuery(options);
  return {
    interaction: mergeEventBodies(interaction, data),
    loading: options.enabled && isLoading,
    failed: options.enabled && isError,
  };
}

/** Start loading a row's full bodies before it is expanded (hover, focus). */
export function prefetchToolBodies(queryClient: QueryClient, interaction: ToolInteraction): void {
  const options = bodiesQueryOptions(interaction);
  if (!options.enabled) return;
  void queryClient.prefetchQuery(options).catch(() => undefined);
}

/** Hover and focus handlers that warm a row's full bodies; none when there is nothing to load. */
export function useToolBodyPrefetch(interaction: ToolInteraction): {
  onPointerEnter?: () => void;
  onFocus?: () => void;
} {
  const queryClient = useContext(QueryClientContext);
  if (!queryClient || !hasLiteBodies(interaction)) return {};
  const warm = () => prefetchToolBodies(queryClient, interaction);
  return { onPointerEnter: warm, onFocus: warm };
}
