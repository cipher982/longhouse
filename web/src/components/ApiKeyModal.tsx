/**
 * ApiKeyModal - Shown when user tries to access Chat without an API key configured
 *
 * Provides a friendly prompt to add an OpenAI or Anthropic API key,
 * with option to skip and continue browsing.
 */

import { Button } from "./ui";

interface ApiKeyModalProps {
  isOpen: boolean;
  onClose: () => void;
  onOpenIntegrations: () => void;
}

export function ApiKeyModal({
  isOpen,
  onClose,
  onOpenIntegrations,
}: ApiKeyModalProps) {
  if (!isOpen) return null;

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div
        className="modal-container api-key-modal"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-header">
          <h2>API Key Required</h2>
          <button
            className="modal-close-button"
            onClick={onClose}
            aria-label="Close"
          >
            &times;
          </button>
        </div>

        <div className="modal-content">
          <p className="api-key-modal__description">
            To chat with Oikos, you need to configure an LLM API key.
          </p>

          <div className="api-key-modal__options">
            <button
              type="button"
              className="api-key-modal__option"
              onClick={onOpenIntegrations}
            >
              <h3>OpenAI</h3>
              <p>Use GPT models for chat. Get a key at openai.com</p>
            </button>
            <button
              type="button"
              className="api-key-modal__option"
              onClick={onOpenIntegrations}
            >
              <h3>Anthropic</h3>
              <p>Use Claude models for chat. Get a key at anthropic.com</p>
            </button>
          </div>

          <p className="api-key-modal__note">
            You can still browse Timeline and view past sessions without an API key.
          </p>
        </div>

        <div className="modal-actions">
          <Button variant="secondary" onClick={onClose}>
            Skip for now
          </Button>
          <Button variant="primary" onClick={onOpenIntegrations}>
            Open Integrations
          </Button>
        </div>
      </div>
    </div>
  );
}
