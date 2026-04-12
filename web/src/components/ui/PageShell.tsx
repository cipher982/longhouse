import React, { useRef } from 'react';
import clsx from 'clsx';
import { useScrollActivity } from '../../hooks/useScrollActivity';

type PageShellSize = 'narrow' | 'normal' | 'wide' | 'full';

interface PageShellProps {
  size?: PageShellSize;
  className?: string;
  children: React.ReactNode;
  /** Called on each scroll-activity event — use to gate hover-intent logic. */
  onScrollActivity?: () => void;
}

export const PageShell: React.FC<PageShellProps> = ({
  size = 'normal',
  className,
  children,
  onScrollActivity,
}) => {
  const shellRef = useRef<HTMLDivElement>(null);

  useScrollActivity(
    () => shellRef.current,
    {
      scrollClass: 'page-shell--scrolling',
      rootClass: 'react-root--scrolling',
      onActivity: onScrollActivity,
    }
  );

  return (
    <div ref={shellRef} className="page-shell">
      <div className={clsx('page-shell-content', `page-shell-content--${size}`, className)}>
        {children}
      </div>
    </div>
  );
};
