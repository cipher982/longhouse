import { useEffect, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";

interface SessionInfoDrawerProps {
  open: boolean;
  onClose: () => void;
  title?: string;
  children: ReactNode;
}

export function SessionInfoDrawer({
  open,
  onClose,
  title = "Session details",
  children,
}: SessionInfoDrawerProps) {
  const panelRef = useRef<HTMLDivElement | null>(null);
  const previouslyFocusedRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!open) return;
    previouslyFocusedRef.current = document.activeElement as HTMLElement | null;
    const node = panelRef.current;
    if (node) {
      const focusable = node.querySelector<HTMLElement>(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
      );
      (focusable ?? node).focus({ preventScroll: true });
    }
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
      }
    };
    document.addEventListener("keydown", handleKey);
    return () => {
      document.removeEventListener("keydown", handleKey);
      previouslyFocusedRef.current?.focus?.({ preventScroll: true });
    };
  }, [open, onClose]);

  if (!open) return null;

  // On body, not in the page: #app-container is its own stacking context
  // under the app header, so a drawer rendered inside it sat beneath the
  // header's controls whatever its z-index.
  return createPortal(
    <div
      className="session-info-drawer"
      role="dialog"
      aria-modal="true"
      aria-label={title}
      data-testid="session-info-drawer"
    >
      <div
        className="session-info-drawer__scrim"
        onClick={onClose}
        aria-hidden="true"
      />
      <div
        className="session-info-drawer__panel"
        ref={panelRef}
        tabIndex={-1}
      >
        <div className="session-info-drawer__header">
          <span className="session-info-drawer__title">{title}</span>
          <button
            type="button"
            className="session-info-drawer__close"
            onClick={onClose}
            aria-label="Close session details"
          >
            ×
          </button>
        </div>
        <div className="session-info-drawer__body">{children}</div>
      </div>
    </div>,
    document.body,
  );
}
