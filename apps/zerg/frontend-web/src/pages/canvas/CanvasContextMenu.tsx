import React, { memo, useRef, useEffect } from "react";

interface CanvasContextMenuProps {
  x: number;
  y: number;
  nodeId: string;
  onDuplicate: () => void;
  onDelete: () => void;
  onClose: () => void;
}

function CanvasContextMenuComponent({
  x,
  y,
  nodeId,
  onDuplicate,
  onDelete,
  onClose,
}: CanvasContextMenuProps) {
  const menuRef = useRef<HTMLDivElement | null>(null);

  // Handle click outside and escape key
  useEffect(() => {
    const handlePointer = (event: MouseEvent) => {
      if (menuRef.current?.contains(event.target as Node)) {
        return;
      }
      onClose();
    };

    const handleEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
      }
    };

    window.addEventListener("mousedown", handlePointer);
    window.addEventListener("contextmenu", handlePointer);
    window.addEventListener("keydown", handleEscape);

    return () => {
      window.removeEventListener("mousedown", handlePointer);
      window.removeEventListener("contextmenu", handlePointer);
      window.removeEventListener("keydown", handleEscape);
    };
  }, [onClose]);

  // Focus menu when mounted
  useEffect(() => {
    if (menuRef.current) {
      menuRef.current.focus();
    }
  }, []);

  return (
    <div
      ref={menuRef}
      className="canvas-context-menu"
      role="menu"
      tabIndex={-1}
      style={{ top: y, left: x }}
      data-node-id={nodeId}
    >
      <button type="button" role="menuitem" onClick={onDuplicate}>
        Duplicate node
      </button>
      <button type="button" role="menuitem" onClick={onDelete}>
        Delete node
      </button>
    </div>
  );
}

// Wrap with React.memo for performance optimization
export const CanvasContextMenu = memo(CanvasContextMenuComponent);
