import { useEffect, useRef } from "react";
import clsx from "clsx";
import { ThreadMessage } from "../../services/api";
import { formatTimestamp } from "./chatUtils";
import { ToolMessage } from "./ToolMessage";

interface ChatMessageListProps {
  messages: ThreadMessage[];
  streamingMessages: Map<number, string>;
  streamingMessageId: number | null;
  pendingTokenBuffer: string;
  onCopyMessage: (message: ThreadMessage) => void;
}

export function ChatMessageList({
  messages,
  streamingMessages,
  streamingMessageId,
  pendingTokenBuffer,
  onCopyMessage,
}: ChatMessageListProps) {
  const messagesContainerRef = useRef<HTMLDivElement>(null);

  // Scroll to bottom when messages change
  useEffect(() => {
    if (messagesContainerRef.current) {
      messagesContainerRef.current.scrollTop = messagesContainerRef.current.scrollHeight;
    }
  }, [messages, pendingTokenBuffer, streamingMessages]);

  // Group tool messages by parent_id for rendering under assistant messages
  const toolMessagesByParent = new Map<number, ThreadMessage[]>();
  messages
    .filter(m => m.role === "tool" && m.parent_id != null)
    .forEach(msg => {
      const list = toolMessagesByParent.get(msg.parent_id!) || [];
      list.push(msg);
      toolMessagesByParent.set(msg.parent_id!, list);
    });

  // Get orphaned tool messages (no parent_id)
  const orphanedToolMessages = messages.filter(m => m.role === "tool" && m.parent_id == null);

  return (
    <section className="conversation-area">
      <div className="messages-container" data-testid="messages-container" ref={messagesContainerRef}>
        {messages
          .filter(msg => msg.role !== "system" && msg.role !== "tool")
          .map((msg, index) => {
            const isLastUserMessage = msg.role === "user" && index === messages.length - 1;
            const toolMessages = toolMessagesByParent.get(msg.id);

            // Check if this message is currently streaming
            const streamingContent = streamingMessages.get(msg.id);
            const isStreaming = streamingMessageId === msg.id && streamingContent !== undefined;
            const displayContent = streamingContent !== undefined ? streamingContent : msg.content;

            // Skip rendering empty assistant messages (they only have tool calls)
            if (msg.role === "assistant" && msg.content.trim() === "" && !isStreaming) {
              return (
                <div key={msg.id}>
                  {toolMessages?.map(toolMsg => (
                    <ToolMessage key={toolMsg.id} message={toolMsg} />
                  ))}
                </div>
              );
            }

            return (
              <div key={msg.id}>
                <div className="chat-row">
                  <article
                    className={clsx("message", {
                      "user-message": msg.role === "user",
                      "assistant-message": msg.role === "assistant",
                      "streaming": isStreaming,
                    })}
                    data-testid={isLastUserMessage ? "chat-message" : undefined}
                    data-role={`chat-message-${msg.role}`}
                    data-streaming={isStreaming ? "true" : undefined}
                  >
                    <div className="message-content preserve-whitespace">
                      {displayContent || (isStreaming ? "" : msg.content)}
                      {isStreaming && <span className="streaming-cursor">▋</span>}
                    </div>
                    <div className="message-footer">
                      <div className="message-time">{formatTimestamp(msg.created_at)}</div>
                      <div className="message-actions">
                        <button
                          type="button"
                          className="message-action-btn"
                          onClick={() => onCopyMessage(msg)}
                          title="Copy message"
                        >
                          📋
                        </button>
                      </div>
                    </div>
                  </article>
                </div>
                {msg.role === "assistant" && toolMessages?.map(toolMsg => (
                  <ToolMessage key={toolMsg.id} message={toolMsg} />
                ))}
              </div>
            );
          })}
        {orphanedToolMessages.map(toolMsg => (
          <ToolMessage key={toolMsg.id} message={toolMsg} />
        ))}
        {/* Show pending buffer as temporary assistant message at END of messages */}
        {pendingTokenBuffer && (
          <div key="pending-stream">
            <div className="chat-row">
              <article
                className="message assistant-message streaming"
                data-streaming="true"
              >
                <div className="message-content preserve-whitespace">
                  {pendingTokenBuffer}
                  <span className="streaming-cursor">▋</span>
                </div>
              </article>
            </div>
          </div>
        )}
        {messages.length === 0 && (
          <p className="thread-list-empty">No messages yet.</p>
        )}
      </div>
    </section>
  );
}
