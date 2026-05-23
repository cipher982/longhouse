import { useRef } from "react";

import { COMPOSER_ATTACHMENT_LIMITS, type ComposerAttachment } from "../lib/useComposerAttachments";

const ACCEPT = Array.from(COMPOSER_ATTACHMENT_LIMITS.allowedMime).join(",");

interface Props {
  attachments: ComposerAttachment[];
  onAddFiles: (files: FileList | File[]) => void;
  onRemove: (clientId: string) => void;
  isCompressing?: boolean;
  error?: string | null;
  onClearError?: () => void;
  disabled?: boolean;
  addDisabled?: boolean;
}

export function AttachmentTray({
  attachments,
  onAddFiles,
  onRemove,
  isCompressing = false,
  error,
  onClearError,
  disabled,
  addDisabled,
}: Props) {
  const inputRef = useRef<HTMLInputElement>(null);
  const slotsLeft = COMPOSER_ATTACHMENT_LIMITS.maxAttachments - attachments.length;
  const canAdd = !disabled && !addDisabled && slotsLeft > 0;

  return (
    <div className="session-chat-attachment-tray" data-testid="attachment-tray">
      {attachments.map((a) => (
        <div key={a.clientId} className="session-chat-attachment-tray__item">
          <img src={a.previewUrl} alt={a.filename} className="session-chat-attachment-tray__thumb" />
          <button
            type="button"
            className="session-chat-attachment-tray__remove"
            onClick={() => onRemove(a.clientId)}
            aria-label={`Remove ${a.filename}`}
            disabled={disabled}
          >
            ×
          </button>
        </div>
      ))}
      {canAdd ? (
        <button
          type="button"
          className="session-chat-attachment-tray__add"
          onClick={() => inputRef.current?.click()}
          disabled={isCompressing}
          aria-label="Attach images"
        >
          {isCompressing ? "…" : "+"}
        </button>
      ) : null}
      <input
        ref={inputRef}
        type="file"
        accept={ACCEPT}
        multiple
        hidden
        onChange={(e) => {
          if (e.target.files?.length) onAddFiles(e.target.files);
          e.target.value = "";
        }}
      />
      {error ? (
        <div
          className="session-chat-attachment-tray__error"
          role="alert"
          onClick={onClearError}
        >
          {error}
        </div>
      ) : null}
    </div>
  );
}
