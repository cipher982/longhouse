import type { FormEvent } from "react";
import type { ConnectorStatus, CredentialField } from "../../types/connectors";
import { Button } from "../ui";

export type ConfigModalState = {
  isOpen: boolean;
  connector: ConnectorStatus | null;
  credentials: Record<string, string>;
  displayName: string;
};

type ConnectorConfigModalProps = {
  modal: ConfigModalState;
  onClose: () => void;
  onSave: (e: FormEvent) => void;
  onTest: () => void;
  onCredentialChange: (key: string, value: string) => void;
  onDisplayNameChange: (value: string) => void;
  isSaving: boolean;
  isTesting: boolean;
};

export function ConnectorConfigModal({
  modal,
  onClose,
  onSave,
  onTest,
  onCredentialChange,
  onDisplayNameChange,
  isSaving,
  isTesting,
}: ConnectorConfigModalProps) {
  if (!modal.isOpen || !modal.connector) return null;

  return (
    <div className="connector-modal-backdrop" onClick={onClose} role="presentation">
      <div className="connector-modal" onClick={(e) => e.stopPropagation()}>
        <header className="connector-modal-header">
          <h3>Configure {modal.connector.name}</h3>
          <button type="button" className="close-btn" onClick={onClose} aria-label="Close">
            ×
          </button>
        </header>

        <form onSubmit={onSave}>
          <div className="connector-modal-body">
            <p className="connector-description">{modal.connector.description}</p>

            <a
              href={modal.connector.docs_url}
              target="_blank"
              rel="noopener noreferrer"
              className="connector-docs-link"
            >
              Setup documentation →
            </a>

            <div className="connector-fields">
              <label className="form-field">
                Display Name (optional)
                <input
                  type="text"
                  value={modal.displayName}
                  onChange={(e) => onDisplayNameChange(e.target.value)}
                  placeholder="e.g. #engineering channel"
                />
              </label>

              {modal.connector.fields.map((field) => (
                <CredentialFieldInput
                  key={field.key}
                  field={field}
                  value={modal.credentials[field.key] ?? ""}
                  onChange={(v) => onCredentialChange(field.key, v)}
                />
              ))}
            </div>
          </div>

          <footer className="connector-modal-footer">
            <Button type="button" variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button
              type="button"
              variant="tertiary"
              onClick={onTest}
              disabled={isTesting}
            >
              {isTesting ? "Testing…" : "Test"}
            </Button>
            <Button
              type="submit"
              variant="primary"
              disabled={isSaving}
            >
              {isSaving ? "Saving…" : "Save"}
            </Button>
          </footer>
        </form>
      </div>
    </div>
  );
}

type CredentialFieldInputProps = {
  field: CredentialField;
  value: string;
  onChange: (value: string) => void;
};

export function CredentialFieldInput({ field, value, onChange }: CredentialFieldInputProps) {
  return (
    <label className="form-field">
      {field.label}
      {field.required && <span className="required">*</span>}
      <input
        type={field.type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={field.placeholder}
        required={field.required}
      />
    </label>
  );
}
