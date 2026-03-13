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
import { getAuthMethods, useAuth } from "../lib/auth";
import { config } from "../lib/config";
import { requestGoogleAuthorizationCode } from "../lib/googleCodeClient";
import {
  connectGmailInbox,
  fetchConversation,
  fetchConversationMessages,
  fetchConversations,
  replyToConversation,
  searchConversations,
  type CanonicalConversationMessage,
  type CanonicalConversationSummary,
} from "../services/api";
import { startHostedGmailConnect } from "../services/api/auth";
import "../styles/conversations.css";

const DEFAULT_LIMIT = 50;
const GMAIL_CONNECT_SCOPE = [
  "https://www.googleapis.com/auth/gmail.modify",
  "https://www.googleapis.com/auth/gmail.send",
].join(" ");

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

function formatGmailConnectError(error: unknown): string {
  if (!(error instanceof Error)) {
    return "Could not connect Gmail.";
  }

  if (error.message.includes("No refresh_token in Google response")) {
    return "Google did not return long-lived inbox access. Remove Longhouse from your Google account permissions, then try connecting again.";
  }

  return error.message;
}

export default function ConversationsPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const queryClient = useQueryClient();
  const { user, refreshAuth } = useAuth();

  const searchQuery = searchParams.get("q") ?? "";
  const selectedConversationId = Number(searchParams.get("conversation") || 0) || null;
  const [draftQuery, setDraftQuery] = useState(searchQuery);
  const [replyBody, setReplyBody] = useState("");
  const [replyAll, setReplyAll] = useState(false);
  const gmailErrorParam = searchParams.get("gmail_error");

  const authMethodsQuery = useQuery({
    queryKey: ["auth-methods"],
    queryFn: getAuthMethods,
    staleTime: 5 * 60 * 1000,
  });

  const usesHostedGmailConnect = Boolean(authMethodsQuery.data?.sso_url);
  const gmailReady = authMethodsQuery.data?.gmail_ready ?? (usesHostedGmailConnect || Boolean(config.googleClientId));
  const gmailSetupMessage = authMethodsQuery.data?.gmail_setup_message ?? null;
  const canConnectGmail = gmailReady && (usesHostedGmailConnect || Boolean(config.googleClientId));

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

  const connectGmailMutation = useMutation({
    mutationFn: async () => {
      if (usesHostedGmailConnect) {
        const result = await startHostedGmailConnect();
        window.location.assign(result.url);
        return { redirected: true as const };
      }
      const authCode = await requestGoogleAuthorizationCode({
        clientId: config.googleClientId,
        scope: GMAIL_CONNECT_SCOPE,
      });
      return connectGmailInbox(authCode);
    },
    onSuccess: async (result) => {
      if ("redirected" in result) {
        return;
      }
      await refreshAuth?.();
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["canonical-conversations"] }),
        queryClient.invalidateQueries({ queryKey: ["canonical-conversation"] }),
        queryClient.invalidateQueries({ queryKey: ["canonical-conversation-messages"] }),
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
  const gmailStatus = user?.gmail_watch_status ?? null;

  const gmailPanel = useMemo(() => {
    if (!user?.gmail_connected) {
      return {
        tone: "neutral",
        title: "Connect Gmail to start your inbox",
        description: gmailSetupMessage
          ?? (usesHostedGmailConnect
          ? "Longhouse will send you to control.longhouse.ai to connect your existing Gmail or Workspace mailbox, then bring you back here."
          : canConnectGmail
          ? "This page becomes your assistant email inbox once Gmail is connected. Oikos will search past threads and only reply inside existing conversations."
          : "Google OAuth is not configured on this instance yet. Add a Google client first, then connect Gmail here."),
        actionLabel: "Connect Gmail",
        showAction: true,
        mailboxLabel: null,
      };
    }

    const mailboxLabel = user.gmail_mailbox_email ? `Connected as ${user.gmail_mailbox_email}` : "Gmail connected";

    if (gmailStatus === "active") {
      return {
        tone: "success",
        title: "Email sync is healthy",
        description: "New Gmail messages will land here automatically, and Oikos can reply in the same thread without leaving the inbox.",
        actionLabel: null,
        showAction: false,
        mailboxLabel,
      };
    }

      return {
      tone: "warning",
      title: "Gmail needs attention",
      description:
        gmailErrorParam
        || user.gmail_watch_error
        || "Reconnect Gmail to restore inbox syncing. Existing threads stay searchable, but new mail may stop landing here.",
      actionLabel: "Reconnect Gmail",
      showAction: true,
      mailboxLabel,
    };
  }, [canConnectGmail, gmailErrorParam, gmailSetupMessage, gmailStatus, user, usesHostedGmailConnect]);

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

  const handleConnectGmail = async () => {
    await connectGmailMutation.mutateAsync();
  };

  const emptyInboxDescription = useMemo(() => {
    if (searchQuery) {
      return "Try a different search.";
    }
    if (!user?.gmail_connected) {
      return gmailSetupMessage
        ?? (usesHostedGmailConnect
        ? "Connect Gmail above and Longhouse will finish the hosted authorization flow on control.longhouse.ai."
        : canConnectGmail
        ? "Connect Gmail above to turn this into your assistant inbox."
        : "Ask the instance admin to configure Google OAuth, then connect Gmail here.");
    }
    if (gmailStatus === "active") {
      return "Email sync is live. Your first Gmail thread will appear here automatically.";
    }
    return "Reconnect Gmail above to restore syncing before new mail can land here.";
  }, [canConnectGmail, gmailSetupMessage, gmailStatus, searchQuery, user?.gmail_connected, usesHostedGmailConnect]);

  return (
    <PageShell size="full" className="conversations-page">
      <SectionHeader
        title="Inbox"
        description="Canonical email threads for Longhouse. Search past conversations, open a thread, and reply in place."
      />

      <section
        className={`gmail-connection-panel gmail-connection-panel--${gmailPanel.tone}`}
        data-testid="gmail-connection-panel"
      >
        <div className="gmail-connection-panel__content">
          <div className="gmail-connection-panel__eyebrow">Email</div>
          <h2>{gmailPanel.title}</h2>
          <p>{gmailPanel.description}</p>
          {gmailPanel.mailboxLabel ? (
            <div className="gmail-connection-panel__meta">{gmailPanel.mailboxLabel}</div>
          ) : null}
          {connectGmailMutation.isError || gmailErrorParam ? (
            <div className="gmail-connection-panel__error">
              {connectGmailMutation.isError
                ? formatGmailConnectError(connectGmailMutation.error)
                : gmailErrorParam}
            </div>
          ) : null}
        </div>
        <div className="gmail-connection-panel__actions">
          {gmailPanel.showAction ? (
            <Button
              type="button"
              variant="primary"
              onClick={handleConnectGmail}
              disabled={!canConnectGmail || connectGmailMutation.isPending}
            >
              {connectGmailMutation.isPending ? "Connecting..." : gmailPanel.actionLabel}
            </Button>
          ) : (
            <div className="gmail-connection-panel__status">Ready</div>
          )}
        </div>
      </section>

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
              description={emptyInboxDescription}
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
                <div className="conversations-reply__hint">
                  Replies go out from your connected Gmail account and stay in the same thread.
                </div>
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
