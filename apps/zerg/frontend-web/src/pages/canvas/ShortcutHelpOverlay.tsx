import React, { memo } from "react";
import { XIcon } from "../../components/icons";

interface ShortcutHelpOverlayProps {
  onClose: () => void;
}

function ShortcutHelpOverlayComponent({ onClose }: ShortcutHelpOverlayProps) {
  return (
    <div className="shortcut-help-overlay" role="dialog" aria-modal="true" aria-labelledby="shortcut-help-title">
      <div className="shortcut-help-panel">
        <div className="shortcut-help-header">
          <h3 id="shortcut-help-title">Canvas Shortcuts</h3>
          <button
            type="button"
            className="close-logs"
            onClick={onClose}
            title="Close shortcuts"
          >
            <XIcon width={14} height={14} />
          </button>
        </div>
        <ul className="shortcut-help-list">
          <li><kbd>Shift</kbd> + <kbd>S</kbd> Toggle snap to grid</li>
          <li><kbd>Shift</kbd> + <kbd>G</kbd> Toggle guides</li>
          <li><kbd>Shift</kbd> + <kbd>/</kbd> Show this panel</li>
        </ul>
        <p className="shortcut-help-hint">Press Esc to close.</p>
      </div>
    </div>
  );
}

// Wrap with React.memo for performance optimization
export const ShortcutHelpOverlay = memo(ShortcutHelpOverlayComponent);
