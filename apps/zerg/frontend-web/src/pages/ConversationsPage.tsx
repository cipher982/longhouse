import { FormEvent, useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import {
  Button,
  EmptyState,
  Input,
  PageShell,
  SectionHeader,
  Spinner,
} from "../components/ui";
import {
  fetchConversation,
  fetchConversationMessages,
  fetchConversations,
  replyToConversation,
  searchConversations,
  type CanonicalConversationMessage,
  type CanonicalConversationSummary,
} from "../services/api";
import "../styles/conversations.css";

const DEFAULT_LIMIT = 50;

function formatConversationTimestamp(value?: string | null): string {
  if (!value) return "No activity yet";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "No activity yet";
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function getSenderLabel(message: CanonicalConversationMessage): string {
  if (message.sender_display) {
    return message.sender_display;
  }
  if (message.direction === "incoming") {
    return "Incoming";
  }
  if (message.sender_kind === "agent") {
    return "Oikos";
  }
  return "You";
}

export default function ConversationsPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const queryClient = useQueryClient();

  const searchQuery = searchParams.get("q") ?? "";
  const selectedConversationId = Number(searchParams.get("conversation") || 0) || null;
  const [draftQuery, setDraftQuery] = useState(searchQuery);
  const [replyBody, setReplyBody] = useState("");
  const [replyAll, setReplyAll] = useState(false);

  useEffect(() => {
    setDraftQuery(searchQuery);
  }, [searchQuery]);

  const listQuery = useQuery({
    queryKey: ["canonical-conversations", searchQuery],
    queryFn: () => {
      if (searchQuery.trim()) {
        return searchConversations(searchQuery.trim(), { kind: "email", limit: DEFAULT_LIMIT });
      }
      return fetchConversations({ kind: "email", limit: DEFAULT_LIMIT });
    },
  });

  const conversations = useMemo(
    () => listQuery.data?.conversations ?? [],
    [listQuery.data?.conversations],
  );

  useEffect(() => {
    const isReady = !listQuery.isLoading;
    if (isReady) {
      document.body.setAttribute("data-ready", "true");
    }
    return () => document.body.removeAttribute("data-ready");
  }, [listQuery.isLoading]);

  useEffect(() => {
    if (conversations.length === 0) {
      return;
    }
    if (selectedConversationId && conversations.some((conversation) => conversation.id === selectedConversationId)) {
      return;
    }
    const nextParams = new URLSearchParams(searchParams);
    nextParams.set("conversation", String(conversations[0].id));
    setSearchParams(nextParams, { replace: true });
  }, [conversations, searchParams, selectedConversationId, setSearchParams]);

  const detailQuery = useQuery({
    queryKey: ["canonical-conversation", selectedConversationId],
    queryFn: () => fetchConversation(selectedConversationId as number),
    enabled: selectedConversationId !== null,
  });

  const messagesQuery = useQuery({
    queryKey: ["canonical-conversation-messages", selectedConversationId],
    queryFn: () => fetchConversationMessages(selectedConversationId as number, { limit: 200 }),
    enabled: selectedConversationId !== null,
  });

  const replyMutation = useMutation({
    mutationFn: () => {
      if (selectedConversationId === null) {
        throw new Error("Select a conversation first");
      }
      return replyToConversation(selectedConversationId, {
        body: replyBody.trim(),
        reply_all: replyAll,
      });
    },
    onSuccess: async () => {
      setReplyBody("");
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["canonical-conversation-messages", selectedConversationId] }),
        queryClient.invalidateQueries({ queryKey: ["canonical-conversation", selectedConversationId] }),
        queryClient.invalidateQueries({ queryKey: ["canonical-conversations", searchQuery] }),
      ]);
    },
  });

  const selectedConversation = useMemo(
    () =>
      conversations.find((conversation) => conversation.id === selectedConversationId)
      ?? detailQuery.data
      ?? null,
    [conversations, detailQuery.data, selectedConversationId],
  );

  const messages = messagesQuery.data?.messages ?? [];

  const handleSearch = (event: FormEvent) => {
    event.preventDefault();
    const trimmed = draftQuery.trim();
    const nextParams = new URLSearchParams(searchParams);
    if (trimmed) {
      nextParams.set("q", trimmed);
    } else {
      nextParams.delete("q");
    }
    nextParams.delete("conversation");
    setSearchParams(nextParams);
  };

  const handleSelectConversation = (conversation: CanonicalConversationSummary) => {
    const nextParams = new URLSearchParams(searchParams);
    nextParams.set("conversation", String(conversation.id));
    setSearchParams(nextParams);
  };

  const handleReply = async (event: FormEvent) => {
    event.preventDefault();
    if (!replyBody.trim()) {
      return;
    }
    await replyMutation.mutateAsync();
  };

  return (
    <PageShell size="full" className="conversations-page">
      <SectionHeader
        title="Inbox"
        description="Canonical email threads for Longhouse. Search past conversations, open a thread, and reply in place."
      />

      <div className="conversations-layout">
        <aside className="conversations-sidebar">
          <form className="conversations-search" onSubmit={handleSearch}>
            <Input
              aria-label="Search conversations"
              placeholder="Search email threads"
              value={draftQuery}
              onChange={(event) => setDraftQuery(event.target.value)}
            />
            <Button type="submit" variant="secondary">
              Search
            </Button>
          </form>

          {listQuery.isLoading ? (
            <div className="conversations-sidebar-empty">
              <Spinner size="md" />
            </div>
          ) : null}

          {listQuery.isError ? (
            <EmptyState
              variant="error"
              title="Could not load conversations"
              description={listQuery.error instanceof Error ? listQuery.error.message : "Unknown error"}
            />
          ) : null}

          {!listQuery.isLoading && !listQuery.isError && conversations.length === 0 ? (
            <EmptyState
              title="No email threads yet"
              description={searchQuery ? "Try a different search." : "Connect Gmail and wait for the first message to land."}
            />
          ) : null}

          <div className="conversations-list">
            {conversations.map((conversation) => (
              <button
                key={conversation.id}
                type="button"
                className={`conversations-list-item${conversation.id === selectedConversationId ? " is-active" : ""}`}
                data-testid={`conversation-item-${conversation.id}`}
                onClick={() => handleSelectConversation(conversation)}
              >
                <div className="conversations-list-item__title">
                  {conversation.title || "(untitled conversation)"}
                </div>
                <div className="conversations-list-item__meta">
                  <span>{conversation.message_count} messages</span>
                  <span>{formatConversationTimestamp(conversation.last_message_at)}</span>
                </div>
              </button>
            ))}
          </div>
        </aside>

        <section className="conversations-thread" data-testid="conversation-thread">
          {!selectedConversation ? (
            <EmptyState
              title="Select a conversation"
              description="Choose a thread from the inbox to read and reply."
            />
          ) : (
            <>
              <div className="conversations-thread__header">
                <div>
                  <h2>{selectedConversation.title || "(untitled conversation)"}</h2>
                  <p>
                    {selectedConversation.message_count} messages
                    {" · "}
                    {formatConversationTimestamp(selectedConversation.last_message_at)}
                  </p>
                </div>
              </div>

              {messagesQuery.isLoading ? (
                <div className="conversations-thread__loading">
                  <Spinner size="lg" />
                </div>
              ) : null}

              {messagesQuery.isError ? (
                <EmptyState
                  variant="error"
                  title="Could not load messages"
                  description={messagesQuery.error instanceof Error ? messagesQuery.error.message : "Unknown error"}
                />
              ) : null}

              {!messagesQuery.isLoading && !messagesQuery.isError ? (
                <div className="conversations-thread__messages">
                  {messages.map((message) => (
                    <article
                      key={message.id}
                      className={`conversation-message conversation-message--${message.direction}`}
                    >
                      <div className="conversation-message__meta">
                        <span className="conversation-message__sender">{getSenderLabel(message)}</span>
                        <span>{formatConversationTimestamp(message.sent_at)}</span>
                      </div>
                      <div className="conversation-message__content">{message.content}</div>
                    </article>
                  ))}
                </div>
              ) : null}

              <form className="conversations-reply" onSubmit={handleReply}>
                <label className="conversations-reply__label" htmlFor="conversation-reply-body">
                  Reply
                </label>
                <textarea
                  id="conversation-reply-body"
                  className="conversations-reply__textarea"
                  value={replyBody}
                  onChange={(event) => setReplyBody(event.target.value)}
                  placeholder="Write your reply"
                  rows={6}
                />
                <label className="conversations-reply__checkbox">
                  <input
                    type="checkbox"
                    checked={replyAll}
                    onChange={(event) => setReplyAll(event.target.checked)}
                  />
                  Reply all
                </label>
                {replyMutation.isError ? (
                  <div className="conversations-reply__error">
                    {replyMutation.error instanceof Error ? replyMutation.error.message : "Reply failed"}
                  </div>
                ) : null}
                <div className="conversations-reply__actions">
                  <Button
                    type="submit"
                    variant="primary"
                    disabled={!replyBody.trim() || replyMutation.isPending}
                    data-testid="conversation-reply-submit"
                  >
                    {replyMutation.isPending ? "Sending..." : "Send reply"}
                  </Button>
                </div>
              </form>
            </>
          )}
        </section>
      </div>
    </PageShell>
  );
}
