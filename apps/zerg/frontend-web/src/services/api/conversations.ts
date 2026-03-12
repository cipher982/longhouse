import { request } from "./base";
import type {
  CanonicalConversationDetail,
  CanonicalConversationListResponse,
  CanonicalConversationMessagesResponse,
  CanonicalConversationReplyRequest,
  CanonicalConversationReplyResponse,
} from "./types";

type ConversationListOptions = {
  kind?: string;
  status?: string;
  limit?: number;
};

type ConversationSearchOptions = {
  kind?: string;
  limit?: number;
};

type ConversationMessageOptions = {
  include_internal?: boolean;
  limit?: number;
  offset?: number;
};

export async function fetchConversations(
  options: ConversationListOptions = {},
): Promise<CanonicalConversationListResponse> {
  const params = new URLSearchParams();
  if (options.kind) params.set("kind", options.kind);
  if (options.status) params.set("status", options.status);
  if (options.limit) params.set("limit", String(options.limit));
  const suffix = params.toString() ? `?${params.toString()}` : "";
  return request<CanonicalConversationListResponse>(`/conversations${suffix}`);
}

export async function searchConversations(
  query: string,
  options: ConversationSearchOptions = {},
): Promise<CanonicalConversationListResponse> {
  const params = new URLSearchParams({ q: query });
  if (options.kind) params.set("kind", options.kind);
  if (options.limit) params.set("limit", String(options.limit));
  return request<CanonicalConversationListResponse>(`/conversations/search?${params.toString()}`);
}

export async function fetchConversation(conversationId: number): Promise<CanonicalConversationDetail> {
  return request<CanonicalConversationDetail>(`/conversations/${conversationId}`);
}

export async function fetchConversationMessages(
  conversationId: number,
  options: ConversationMessageOptions = {},
): Promise<CanonicalConversationMessagesResponse> {
  const params = new URLSearchParams();
  if (options.include_internal) params.set("include_internal", "true");
  if (options.limit) params.set("limit", String(options.limit));
  if (options.offset) params.set("offset", String(options.offset));
  const suffix = params.toString() ? `?${params.toString()}` : "";
  return request<CanonicalConversationMessagesResponse>(`/conversations/${conversationId}/messages${suffix}`);
}

export async function replyToConversation(
  conversationId: number,
  payload: CanonicalConversationReplyRequest,
): Promise<CanonicalConversationReplyResponse> {
  return request<CanonicalConversationReplyResponse>(`/conversations/${conversationId}/reply`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}
