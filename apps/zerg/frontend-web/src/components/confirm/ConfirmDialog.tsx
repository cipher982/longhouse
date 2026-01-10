import { useEffect, useRef } from 'react';
import type { ConfirmVariant } from './types';
import './ConfirmDialog.css';

interface ConfirmDialogProps {
  isOpen: boolean;
  title: string;
  message: string;
  confirmLabel: string;
  cancelLabel: string;
  variant: ConfirmVariant;
  onConfirm: () => void;
  onCancel: () => void;
}

/**
 * Accessible confirmation dialog following WAI-ARIA Dialog pattern.
 *
 * Features:
 * - role="alertdialog" for danger/warning variants, role="dialog" for default
 * - Focus trapped inside dialog
 * - Initial focus on Cancel (least destructive) by default
 * - Esc closes dialog
 * - aria-labelledby + aria-describedby
 * - data-testid attributes for e2e testing
 */
export function ConfirmDialog({
  isOpen,
  title,
  message,
  confirmLabel,
  cancelLabel,
  variant,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const cancelRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);

  // Focus trap and initial focus on cancel button
  useEffect(() => {
    if (!isOpen) return;

    // Focus the cancel button (least destructive action)
    cancelRef.current?.focus();

    // Handle Escape key
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        onCancel();
      }

      // Focus trap
      if (e.key === 'Tab' && dialogRef.current) {
        const focusable = dialogRef.current.querySelectorAll<HTMLElement>(
          'button:not([disabled]), [tabindex]:not([tabindex="-1"])'
        );
        const first = focusable[0];
        const last = focusable[focusable.length - 1];

        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last?.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first?.focus();
        }
      }
    };

    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [isOpen, onCancel]);

  // Prevent body scroll when dialog is open
  useEffect(() => {
    if (isOpen) {
      document.body.style.overflow = 'hidden';
    } else {
      document.body.style.overflow = '';
    }
    return () => {
      document.body.style.overflow = '';
    };
  }, [isOpen]);

  if (!isOpen) return null;

  return (
    <div
      className="ui-confirm-overlay"
      onClick={(e) => {
        // Close on backdrop click
        if (e.target === e.currentTarget) {
          onCancel();
        }
      }}
    >
      <div
        ref={dialogRef}
        role={variant === 'danger' || variant === 'warning' ? 'alertdialog' : 'dialog'}
        aria-modal="true"
        aria-labelledby="confirm-dialog-title"
        aria-describedby="confirm-dialog-description"
        className={`ui-confirm-dialog ui-confirm-dialog--${variant}`}
        data-testid="confirm-dialog"
      >
        <h2 id="confirm-dialog-title" className="ui-confirm-dialog__title">
          {title}
        </h2>
        <p id="confirm-dialog-description" className="ui-confirm-dialog__message">
          {message}
        </p>
        <div className="ui-confirm-dialog__actions">
          <button
            ref={cancelRef}
            type="button"
            className="ui-confirm-dialog__button ui-confirm-dialog__button--cancel"
            onClick={onCancel}
            data-testid="confirm-cancel"
          >
            {cancelLabel}
          </button>
          <button
            type="button"
            className={`ui-confirm-dialog__button ui-confirm-dialog__button--confirm ui-confirm-dialog__button--${variant}`}
            onClick={onConfirm}
            data-testid="confirm-confirm"
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
