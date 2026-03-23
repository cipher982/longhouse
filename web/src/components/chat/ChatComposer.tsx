import { type FormEvent, useRef, useEffect } from "react";
import clsx from "clsx";
import { FileTextIcon } from "../icons";

interface ChatComposerProps {
  draft: string;
  onDraftChange: (draft: string) => void;
  onSend: (evt: FormEvent) => void;
  effectiveThreadId: number | null;
  isSending: boolean;
  messagesCount: number;
  onExportChat: () => void;
}

export function ChatComposer({
  draft,
  onDraftChange,
  onSend,
  effectiveThreadId,
  isSending,
  messagesCount,
  onExportChat,
}: ChatComposerProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Auto-resize textarea
  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
      textareaRef.current.style.height = `${Math.min(textareaRef.current.scrollHeight, 200)}px`;
    }
  }, [draft]);

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      // Submit form
      onSend(e as any);
    }
  };

  return (
    <div className="chat-composer-container">
      <form className="chat-input-form" onSubmit={onSend}>
        <div className="chat-input-tools-left">
          <button
            type="button"
            className="icon-tool-btn"
            onClick={onExportChat}
            disabled={messagesCount === 0}
            title="Export Chat History"
          >
            <FileTextIcon width={18} height={18} />
          </button>
        </div>

        <div className="chat-input-main">
          <textarea
            ref={textareaRef}
            value={draft}
            onChange={(evt) => onDraftChange(evt.target.value)}
            placeholder={effectiveThreadId ? "Type a message..." : "Select a thread to start chatting"}
            className="chat-input-field"
            data-testid="chat-input"
            disabled={!effectiveThreadId}
            onKeyDown={handleKeyDown}
            rows={1}
          />
          <button
            type="submit"
            className={clsx("chat-send-btn", { disabled: !effectiveThreadId })}
            disabled={isSending || !draft.trim() || !effectiveThreadId}
            data-testid="send-message-btn"
            title="Send Message"
          >
             <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <line x1="22" y1="2" x2="11" y2="13"></line>
                <polygon points="22 2 15 22 11 13 2 9 22 2"></polygon>
             </svg>
          </button>
        </div>
      </form>

    </div>
  );
}
