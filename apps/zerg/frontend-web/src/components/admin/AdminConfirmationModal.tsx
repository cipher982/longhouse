import React, { useCallback, useState } from "react";
import { createPortal } from "react-dom";
import { useBodyScrollLock } from "../../hooks/useBodyScrollLock";
import { useEscapeKey } from "../../hooks/useEscapeKey";
import { Button, Card } from "../ui";

interface AdminConfirmationModalProps {
  isOpen: boolean;
  onClose: () => void;
  onConfirm: (password?: string) => void;
  title: string;
  message: string;
  confirmText?: string;
  isDangerous?: boolean;
  requirePassword?: boolean;
}

export default function AdminConfirmationModal({
  isOpen,
  onClose,
  onConfirm,
  title,
  message,
  confirmText = "Confirm",
  isDangerous = false,
  requirePassword = false,
}: AdminConfirmationModalProps) {
  const [password, setPassword] = useState("");

  const handleClose = useCallback(() => {
    setPassword("");
    onClose();
  }, [onClose]);

  const handleConfirm = useCallback(() => {
    onConfirm(requirePassword ? password : undefined);
    setPassword("");
  }, [onConfirm, password, requirePassword]);

  useBodyScrollLock(isOpen);
  useEscapeKey(() => {
    handleClose();
  }, isOpen);

  if (!isOpen) {
    return null;
  }

  const handleBackdropClick = (event: React.MouseEvent) => {
    if (event.target === event.currentTarget) {
      handleClose();
    }
  };

  return createPortal(
    <div className="admin-confirm-overlay" onClick={handleBackdropClick}>
      <Card className="admin-confirm-card" onClick={(event: React.MouseEvent) => event.stopPropagation()}>
        <Card.Header>
          <h3 className="admin-confirm-title">{title}</h3>
        </Card.Header>
        <Card.Body>
          <p>{message}</p>
          {requirePassword && (
            <div className="form-group admin-confirm-field">
              <input
                type="password"
                className="ui-input"
                placeholder="Confirmation password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && password) {
                    handleConfirm();
                  }
                }}
                autoFocus
              />
            </div>
          )}
          <div className="modal-actions admin-confirm-actions">
            <Button variant="ghost" onClick={handleClose}>
              Cancel
            </Button>
            <Button
              variant={isDangerous ? "danger" : "primary"}
              onClick={handleConfirm}
              disabled={requirePassword && !password}
            >
              {confirmText}
            </Button>
          </div>
        </Card.Body>
      </Card>
    </div>,
    document.body,
  );
}
