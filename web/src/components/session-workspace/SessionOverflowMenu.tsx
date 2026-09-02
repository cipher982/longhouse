import { useRef, useState } from "react";
import clsx from "clsx";
import { EllipsisIcon } from "../icons";
import { useClickOutside } from "../../hooks/useClickOutside";
import { useEscapeKey } from "../../hooks/useEscapeKey";

export interface SessionOverflowMenuItem {
  key: string;
  label: string;
  onSelect: () => void;
  disabled?: boolean;
  danger?: boolean;
  testId?: string;
}

interface SessionOverflowMenuProps {
  items: SessionOverflowMenuItem[];
  label?: string;
  testId?: string;
}

/**
 * The one trailing control on the session header. Session details, timeline
 * visibility, and archive are each used about once per session; giving each
 * its own button crowded the title out of its own bar.
 */
export function SessionOverflowMenu({
  items,
  label = "More actions",
  testId,
}: SessionOverflowMenuProps) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);

  useClickOutside({
    enabled: open,
    refs: [rootRef],
    onClickOutside: () => setOpen(false),
  });
  useEscapeKey(() => setOpen(false), open);

  return (
    <div className="session-overflow-menu" ref={rootRef}>
      <button
        type="button"
        className="ui-button ui-button--ghost ui-button--sm session-overflow-menu__toggle"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={label}
        title={label}
        data-testid={testId}
        onClick={() => setOpen((value) => !value)}
      >
        <EllipsisIcon width={16} height={16} />
      </button>
      {open ? (
        <div className="session-overflow-menu__list" role="menu" aria-label={label}>
          {items.map((item) => (
            <button
              key={item.key}
              type="button"
              role="menuitem"
              className={clsx(
                "session-overflow-menu__item",
                item.danger && "session-overflow-menu__item--danger",
              )}
              disabled={item.disabled}
              data-testid={item.testId}
              onClick={() => {
                setOpen(false);
                item.onSelect();
              }}
            >
              {item.label}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}
